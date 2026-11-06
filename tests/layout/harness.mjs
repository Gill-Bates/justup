//
// tests/layout/harness.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Shared app, seed and audit helpers used by the Playwright Test layout specifications.
import { spawn, spawnSync } from "node:child_process";
import assert from "node:assert/strict";
import AxeBuilder from "@axe-core/playwright";
import { mkdirSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import net from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { devices } from "playwright";
import { resolvePythonExecutable } from "./test-support.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, "../..");
// Absolute per-edge tolerance (CSS px, also at 200 % zoom) for the dashboard edit-mode geometry
// contract in DESIGN.md ("Edit Mode Geometry Contract"). Do not widen it.
const GEOMETRY_TOL_PX = 1;

const profile = process.env.JUSTUP_LAYOUT_PROFILE || "quick";

// Effective CSS viewports. Browser zoom Z on a W x H window gives a (W/Z) x (H/Z) CSS viewport at
// device scale factor Z: identical layout, media queries and rem sizes as real zoom.
const zoom = (w, h, z) => ({ name: `${w}x${h}@${z * 100}%`, w: Math.round(w / z), h: Math.round(h / z), dsf: z });
const plain = (w, h) => ({ name: `${w}x${h}`, w, h, dsf: 1 });
const device = (name) => {
	const descriptor = devices[name];
	if (!descriptor) return { name, missing: true };
	return { name, w: descriptor.viewport.width, h: descriptor.viewport.height, dsf: descriptor.deviceScaleFactor, descriptor };
};
const VIEWPORTS = profile === "full"
	? [plain(1024, 768), plain(1280, 720), plain(1366, 768), plain(1440, 900), plain(1920, 1080), plain(768, 1024), plain(390, 844),
	plain(1536, 864), plain(320, 640), plain(375, 667), plain(1366, 1024), plain(1024, 1366),
	device("iPad Pro 11 landscape"), device("iPad Pro 11"), zoom(1920, 1080, 1.25), zoom(1920, 1080, 1.5), zoom(1920, 1080, 2), zoom(1280, 720, 2), zoom(1366, 768, 1.25)]
	: [plain(1024, 768), plain(1280, 720), plain(1366, 768), plain(1920, 1080), plain(390, 844),
	plain(320, 640), device("iPad Pro 11 landscape"), device("iPad Pro 11"), zoom(1920, 1080, 1.5), zoom(1280, 720, 2)];
// Boundary widths around the 992px sidebar breakpoint and the 576px phone breakpoint.
if (profile === "full") VIEWPORTS.push(plain(992, 700), plain(1000, 700), plain(1100, 700));

// Deliberate component decisions: horizontal scrolling that is allowed to stay local.
const selectedViewports = process.env.JUSTUP_LAYOUT_VIEWPORTS;
if (selectedViewports) {
	const only = selectedViewports.split(",");
	const unknown = only.filter((name) => !VIEWPORTS.some((vp) => vp.name === name));
	if (unknown.length) throw new Error(`Unknown layout viewport(s) for ${profile}: ${unknown.join(", ")}`);
	VIEWPORTS.splice(0, VIEWPORTS.length, ...VIEWPORTS.filter((v) => only.includes(v.name)));
}
const CFG = {
	scrollX: [".settings-tabs", ".nav-tabs", ".ju-notif-tabs", ".table-responsive", ".ju-table-viewport", "pre", ".leaflet-container"],
	revealed: [".ju-notif-swipe-item"],
	clipX: [".leaflet-container", ".progress"],
	// Declared overflow areas inside dashboard widgets: the downtime list is capped and scrolls on its own.
	widgetScroll: [".dashboard-downtime-table"],
};

async function waitForVisualStability(page) {
	const running = await page.evaluate(async () => {
		await document.fonts.ready;
		await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
		// Poll playState: the `finished` promise of a CSS transition can stay pending in headless Chromium.
		const settle = (animation) => new Promise((resolve) => {
			const start = performance.now();
			const tick = () => (animation.playState !== "running" || performance.now() - start > 3000
				? resolve(animation.playState === "running") : requestAnimationFrame(tick));
			tick();
		});
		const finite = document.getAnimations().filter((animation) => animation.playState === "running"
			&& Number.isFinite(animation.effect?.getComputedTiming().endTime));
		return (await Promise.all(finite.map(settle))).filter(Boolean).length;
	});
	if (running) throw new Error(`${running} finite animation(s) still running after 3s`);
}

async function assertBranding(page, pageId, viewportWidth, theme) {
	const state = await page.evaluate(() => {
		const details = (element) => {
			if (!element) return null;
			const rect = element.getBoundingClientRect();
			return { display: getComputedStyle(element).display, filter: getComputedStyle(element).filter, left: rect.left, right: rect.right, width: rect.width };
		};
		return {
			topbar: details(document.querySelector(".ju-topbar-brand .ju-logo")),
			sidebarLogo: details(document.querySelector(".ju-sidebar-brand .ju-logo")),
			sidebarMark: details(document.querySelector(".ju-brand-mark")),
			sidebar: details(document.querySelector(".ju-sidebar")),
			loginCardShadow: document.querySelector(".login-card") && getComputedStyle(document.querySelector(".login-card")).boxShadow,
			loginLogo: details(document.querySelector(".login-card .ju-logo")),
		};
	});
	if (pageId === "dashboard") {
		assert.ok(state.topbar && state.topbar.width > 0, "topbar wordmark is not visible");
		assert.equal(state.sidebarLogo?.display, "none", "sidebar wordmark should be replaced by the compact mark");
		assert.notEqual(state.sidebarMark?.display, "none", "compact sidebar mark is hidden");
		if (viewportWidth >= 992) assert.ok(state.topbar.left >= state.sidebar.right - 1, "wordmark is not to the right of the sidebar");
		assert.ok(state.topbar.filter.includes("drop-shadow"), "topbar wordmark has no shared shadow");
		assert.equal(state.topbar.filter.includes("invert(1)"), theme === "dark", "topbar wordmark theme treatment is incorrect");
	} else {
		assert.ok(state.loginLogo && state.loginLogo.filter.includes("drop-shadow"), "login wordmark has no shared shadow");
		assert.notEqual(state.loginCardShadow, "none", "login panel shadow is missing");
		assert.equal(state.loginLogo.filter.includes("invert(1)"), theme === "dark", "login wordmark theme treatment is incorrect");
	}
}

const settingsTab = (id, extra) => ({
	id: `settings-${id}`,
	path: "/ui/settings",
	modals: id === "general",
	shot: ["users", "recipients", "spooler"].includes(id),
	async prepare(page) {
		await page.click(`#${id}-tab`);
		await page.waitForFunction((pane) => {
			const el = document.querySelector(pane);
			return el && el.classList.contains("show") && el.children.length > 0 && !el.querySelector(":scope > .text-center > .spinner-border");
		}, `#${id}-pane`, { timeout: 10000 });
		if (extra) await extra(page);
		await waitForVisualStability(page);
	},
});
async function waitForTargetDashboard(page) {
	await page.waitForFunction(() => {
		const config = document.getElementById("target-config");
		if (!config || document.querySelector('.target-detail-page [aria-busy="true"]')) return false;
		const expectedCharts = ["chart-uptime"];
		if (["enableHttp", "enablePing", "enableTcp"].some((key) => config.dataset[key] === "true")) expectedCharts.push("chart-combined");
		if (config.dataset.enableHttp === "true") expectedCharts.push("chart-status-code");
		const chartReady = expectedCharts.every((id) => {
			const canvas = document.getElementById(id);
			if (!canvas) return false;
			const label = canvas.getAttribute("aria-label") || "";
			return !/Loading data/i.test(label) && (Boolean(window.Chart?.getChart(canvas)) || /no data/i.test(label));
		});
		const downtime = document.getElementById("downtime-history-body");
		const downtimeReady = Boolean(downtime) && !downtime.querySelector(".target-skeleton, .spinner-border");
		return chartReady && downtimeReady;
	}, null, { timeout: 15000 });
}
async function inspectTargetDashboard(page) {
	return page.evaluate(() => {
		const rect = (element) => {
			if (!element) return null;
			const r = element.getBoundingClientRect();
			return { x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height), right: Math.round(r.right), bottom: Math.round(r.bottom) };
		};
		const node = (selector) => document.querySelector(selector);
		const chart = (id) => {
			const canvas = document.getElementById(id);
			const instance = canvas && window.Chart?.getChart(canvas);
			return {
				canvas: rect(canvas),
				plot: rect(canvas?.parentElement),
				chartArea: instance?.chartArea || null,
				legend: instance?.legend ? { left: instance.legend.left, top: instance.legend.top, right: instance.legend.right, bottom: instance.legend.bottom, width: instance.legend.width, height: instance.legend.height, position: instance.legend.options.position } : null,
			};
		};
		const scroll = (element) => element && ({ clientHeight: element.clientHeight, scrollHeight: element.scrollHeight, overflowY: getComputedStyle(element).overflowY });
		const combined = node("#combined-card");
		const downtimeBody = node("#downtime-history-card > .card-body");
		const kpiRow = node(".target-kpi-row");
		return {
			viewport: { width: innerWidth, height: innerHeight },
			kpi: {
				row: rect(kpiRow),
				groups: Array.from(kpiRow?.querySelectorAll(":scope > .target-kpi-group") || []).map((group) => ({
					rect: rect(group), cards: Array.from(group.querySelectorAll(":scope > .target-kpi-card")).map(rect),
				})),
			},
			combinedCard: rect(combined), combinedBody: rect(combined?.querySelector(":scope > .card-body")),
			combinedBodyScroll: scroll(combined?.querySelector(":scope > .card-body")),
			combinedLegend: rect(node("#combined-legend")), combinedPlot: rect(node("#combined-plot")),
			combinedFooter: rect(node(".combined-footer")), combinedStatusNote: rect(node("#combined-status-note")), combinedSummary: rect(node("#combined-summary")),
			combinedChart: chart("chart-combined"), availabilityCard: rect(node("#availability-card")),
			availabilityPlot: rect(node("#availability-plot")), availabilityChart: chart("chart-uptime"),
			downtimeCard: rect(node("#downtime-history-card")), downtimeBody: rect(downtimeBody), downtimeBodyScroll: scroll(downtimeBody),
			downtimeFragment: rect(node("#downtime-history-body")), downtimeState: rect(node("#downtime-history-body .downtime-empty, #downtime-history-body .downtime-scroll")),
		};
	});
}
const PAGES = [
	{ id: "login", path: "/ui/login", anon: true },
	{
		id: "dashboard", path: "/", modals: true, shot: true,
		async prepare(page) {
			await page.waitForFunction(() => {
				const grid = document.getElementById("dashboard-grid");
				const loading = document.getElementById("downtime-loading");
				return Boolean(grid?.gridstack) && !grid.classList.contains("dashboard-grid--loading") && (!loading || loading.hidden || loading.classList.contains("d-none"));
			}, null, { timeout: 15000 });
			await page.waitForFunction(() => {
				const tile = document.querySelector('[data-widget-id="delivery-queue"]');
				const badge = tile?.querySelector(".dashboard-monitor-status");
				return tile && !tile.classList.contains("d-none") && badge && badge.textContent.trim() !== "Loading";
			}, null, { timeout: 10000 });
			// The tile height follows its content one animation frame after the status text lands
			// (dashboard-grid.js scheduleFit), so the contract is checked on the settled tile: the
			// body must stop overflowing within the timeout, a permanent ghost scroll still fails.
			const queueBody = page.locator('[data-widget-id="delivery-queue"] .dashboard-monitor-body');
			const measureQueue = () => queueBody.evaluate((body) => ({ scrollHeight: body.scrollHeight, clientHeight: body.clientHeight }));
			const deadline = Date.now() + 5000;
			let queueScroll = await measureQueue();
			while (queueScroll.scrollHeight > queueScroll.clientHeight + 2 && Date.now() < deadline) {
				await page.waitForTimeout(100);
				queueScroll = await measureQueue();
			}
			assert.ok(queueScroll.scrollHeight <= queueScroll.clientHeight + 2, `delivery queue body has ghost scroll: ${queueScroll.scrollHeight}px > ${queueScroll.clientHeight}px`);
		},
	},
	{
		id: "targets", path: "/ui/targets", shot: true,
		async prepare(page) {
			await page.waitForFunction(() => document.querySelector("#targets-table tbody tr") && !document.querySelector("#targets-table.htmx-request"), null, { timeout: 15000 });
		},
	},
	{ id: "target-new", path: "/ui/targets/new" },
	{ id: "target-edit", path: "/ui/targets/1/edit" },
	...["http", "network"].map((tab) => ({
		id: `target-edit-${tab}`, path: "/ui/targets/1/edit",
		async prepare(page) {
			await page.click(`#target-${tab}-tab`);
			await page.waitForSelector(`#target-${tab}-pane.show.active`);
			await waitForVisualStability(page);
		},
	})),
	{ id: "target-dashboard", path: "/ui/targets/1", shot: true },
	{ id: "target-dashboard-kpi", path: "/ui/targets/2", shot: true },
	{ id: "target-dashboard-data", path: "/ui/targets/4", shot: true },
	{
		id: "target-network", path: "/ui/targets/1",
		async prepare(page) {
			await page.click("#target-network-diagnostics-tab");
			await page.waitForFunction(() => {
				const container = document.getElementById("traceroute-container");
				return container?.dataset.loaded === "true" || !!container?.querySelector('[role="alert"]');
			}, null, { timeout: 30000 });
			await waitForVisualStability(page);
		},
	},
	{ id: "groups", path: "/ui/groups", modals: true, shot: true },
	{ id: "notifications", path: "/ui/notifications" },
	{ id: "about", path: "/ui/about" },
	{ id: "change-password", path: "/ui/change-password" },
	...["general", "network", "storage", "backup", "users", "recipients", "signal", "spooler"].map((tab) => settingsTab(tab)),
	{ ...settingsTab("smtp", (page) => page.click("#btn-mode-preview")), id: "settings-smtp-preview", shot: true, sandboxedFrames: true },
	{ ...settingsTab("smtp", (page) => page.click("#btn-mode-edit")), id: "settings-smtp-edit", shot: true, sandboxedFrames: true },
	{ ...settingsTab("smtp", (page) => page.click("#btn-mode-html")), id: "settings-smtp-source", shot: true, sandboxedFrames: true },
];

const free = () => new Promise((resolve, reject) => {
	const server = net.createServer().listen(0, "127.0.0.1", () => { const { port } = server.address(); server.close(() => resolve(port)); });
	server.on("error", reject);
});
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const withDeadline = (promise, ms, label) => {
	let timer;
	const deadline = new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(`${label} timed out after ${ms} ms`)), ms); });
	return Promise.race([promise, deadline]).finally(() => clearTimeout(timer));
};
const cleanups = new Set();
for (const [signal, code] of [["SIGTERM", 143], ["SIGINT", 130]]) {
	process.on(signal, () => {
		console.error(`${signal}: stopping browser and app server`);
		Promise.allSettled([...cleanups].map((cleanup) => cleanup())).finally(() => process.exit(code));
	});
}

async function startApp() {
	const port = await free();
	const data = mkdtempSync(path.join(tmpdir(), "justup-layout-"));
	const python = resolvePythonExecutable(repo);
	const env = {
		...process.env,
		SECRET_KEY: Buffer.from(Array.from({ length: 32 }, () => Math.floor(Math.random() * 256))).toString("base64"),
		JUSTUP_DATA_DIR: data, JUSTUP_HOST: "127.0.0.1", JUSTUP_PORT: String(port), LOG_LEVEL: "WARNING",
		// The first-boot GeoIP download is not needed here; a dead proxy makes it fail instantly.
		HTTPS_PROXY: "http://127.0.0.1:9", HTTP_PROXY: "http://127.0.0.1:9", NO_PROXY: "127.0.0.1,localhost",
	};
	const proc = spawn(python, [path.join(here, "app_stub.py")], { cwd: repo, env, stdio: ["ignore", "pipe", "pipe"] });
	let log = "";
	let spawnError = null;
	proc.on("error", (error) => { spawnError = error; });
	proc.stdout.on("data", (d) => { log += d; });
	proc.stderr.on("data", (d) => { log += d; });
	const base = `http://127.0.0.1:${port}`;
	const stop = async () => {
		cleanups.delete(stop);
		if (!spawnError && proc.exitCode === null && proc.signalCode === null) {
			const exited = new Promise((resolve) => proc.once("exit", resolve));
			const forceKill = setTimeout(() => proc.kill("SIGKILL"), 800);
			proc.kill("SIGTERM");
			await exited;
			clearTimeout(forceKill);
		}
		rmSync(data, { recursive: true, force: true });
	};
	cleanups.add(stop);
	try {
		let ready = false;
		const deadline = Date.now() + 60000;
		while (Date.now() < deadline) {
			if (spawnError) throw new Error(`could not start app with ${python}: ${spawnError.message}`);
			if (proc.exitCode !== null) throw new Error(`app exited early:\n${log.slice(-1500)}`);
			try {
				if ((await fetch(`${base}/ui/login`)).ok) { ready = true; break; }
			} catch { /* not up yet */ }
			await sleep(100);
		}
		if (!ready) throw new Error(`timed out waiting for app startup:\n${log.slice(-1500)}`);
		const credFile = path.join(data, ".bootstrap-admin-credentials");
		const bootstrap = JSON.parse(readFileSync(credFile, "utf8"));
		return { base, bootstrap, data, python, stop };
	} catch (error) {
		await stop();
		throw error;
	}
}

function seedMeasurementFixture(app, targetIds) {
	const script = `from datetime import datetime, timedelta, timezone
from app.db.tsdb import append_point
from app.utils.config import load_config

config = load_config()
now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
start = now - timedelta(minutes=59)
for target_id in ${JSON.stringify(targetIds)}:
	for index in range(60):
		at = start + timedelta(minutes=index)
		http_down = 35 <= index <= 39
		ping_down = 47 <= index <= 49
		values = {
			"http_ms": 110 + (index % 9) * 23,
			"http_up": 0 if http_down else 1,
			"http_status": 503 if http_down else 200,
			"ping_ms": 18 + (index % 7) * 4,
			"ping_up": 0 if ping_down else 1,
			"tcp_ms_443": 35 + (index % 5) * 7,
			"tcp_up_443": 0 if http_down else 1,
			"tcp_ms_8443": 42 + (index % 6) * 8,
			"tcp_up_8443": 0 if ping_down else 1,
		}
		# Isolated spikes so every series has notable peaks (stems) to render.
		spikes = {"http_ms": {12: 900, 28: 1400}, "ping_ms": {20: 260}, "tcp_ms_443": {25: 420}, "tcp_ms_8443": {8: 480, 52: 520}}
		for metric, at_index in spikes.items():
			if index in at_index:
				values[metric] = at_index[index]
		for metric, value in values.items():
			append_point(config.tsdb_dir, target_id=target_id, metric=metric, value=value, retention_days=365, at=at)
`;
	const result = spawnSync(app.python, ["-c", script], {
		cwd: repo,
		env: { ...process.env, JUSTUP_DATA_DIR: app.data },
		encoding: "utf8",
	});
	if (result.error || result.status !== 0) {
		throw new Error(`measurement fixture failed: ${result.error?.message || result.stderr || result.status}`);
	}
}

// Sign in through the UI, replace the bootstrap password and seed one target; returns storage state.
async function seed(browser, app) {
	const ctx = await browser.newContext();
	const page = await ctx.newPage();
	const next = "Layout-Check-Pw-2026!";
	// Every write is recorded with the CSRF token it sent and the cookie the browser attached, so a
	// 403 names the request and the two values instead of only a status list.
	const writes = [];
	page.on("response", async (response) => {
		const request = response.request();
		if (request.method() === "GET") return;
		const headers = await request.allHeaders().catch(() => ({}));
		const cookie = ((headers.cookie || "").match(/csrf_token=([^;]*)/) || [])[1] || "";
		writes.push(`${new URL(request.url()).pathname} ${response.status()} header=${(headers["x-csrf-token"] || "").slice(0, 6)} cookie=${cookie.slice(0, 6)}`);
	});
	await page.goto(`${app.base}/ui/login`);
	await page.fill("#username", app.bootstrap.username);
	await page.fill("#password", app.bootstrap.password);
	await page.click("#login-form button[type=submit]");
	await page.waitForURL(/change-password/);
	await page.fill("#new-password", next);
	await page.fill("#confirm-password", next);
	await page.click("#change-password-form button[type=submit]");
	await page.waitForURL(/login/, { timeout: 10000 });
	await page.fill("#username", app.bootstrap.username);
	await page.fill("#password", next);
	await page.click("#login-form button[type=submit]");
	await page.waitForURL((u) => !/login|change-password/.test(u.pathname), { timeout: 10000 });
	// Seed shapes from the reported defect: a group with short and long names, many TCP ports and
	// long unbreakable recipient values; the table audit has to hold for all of them.
	const targets = [
		{ name: "Example Web", group: "Production", host: "example.com", enable_http_check: true, http_url: "https://example.com/" },
		{ name: "efg-luettringhausen.de", group: "Websites", host: "example.org", enable_ping: true, enable_http_check: true, http_url: "https://example.org/", enable_cert_expiration: true },
		{ name: "wiedenest.de", group: "Websites", host: "example.net", enable_http_check: true, http_url: "https://example.net/" },
		{ name: "a-very-long-hostname-without-break-opportunity.subdomain.example-corporation.com", group: "Websites", host: "example.edu", enable_ping: true, enable_http_check: true, http_url: "https://example.edu/", enable_tcp_connect: true, tcp_host: "example.edu", tcp_ports: "22,25,80,110,143,443,465,587,993,995,3306,5432,8080,8443" },
	];
	const recipient = { name: "Layout Recipient With A Rather Long Display Name", email: "a-very-long-recipient-address-without-breaks@subdomain.example-corporation-international.com", phone: "+4915112345678" };
	const res = await page.evaluate(async ([ts, rc]) => {
		const codes = [];
		let fixtureTargetId = null;
		let screenshotTargetId = null;
		for (const t of ts) {
			const r = await apiFetch("/targets", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(t) });
			if (r.status === 201) {
				const created = await r.json();
				if (t.name === "a-very-long-hostname-without-break-opportunity.subdomain.example-corporation.com") fixtureTargetId = created.id;
				if (t.name === "efg-luettringhausen.de") screenshotTargetId = created.id;
				codes.push(201);
			} else codes.push(`${r.status} ${(await r.text()).slice(0, 200)}`);
		}
		const rr = await apiFetch("/recipients", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(rc) });
		codes.push(rr.status);
		return { codes, fixtureTargetId, screenshotTargetId };
	}, [targets, recipient]);
	if (!res.codes.every((c) => c === 201) || !res.fixtureTargetId || !res.screenshotTargetId) throw new Error(`seeding failed: HTTP ${res.codes.join(",")}; writes: ${writes.join(" | ")}`);
	seedMeasurementFixture(app, [res.screenshotTargetId, res.fixtureTargetId]);
	const state = await ctx.storageState();
	await ctx.close();
	return state;
}

async function auditDashboardGrid(browser, app, adminState, browserName, phase) {
	if (!new Set(["roles", "legacy", "sizes"]).has(phase)) throw new Error(`Unknown GridStack audit phase: ${phase}`);
	const password = "Grid-Layout-2026!";
	const username = `grid-${browserName}-${phase}`;
	const request = (page, endpoint, method = "GET", body) => page.evaluate(async ({ endpoint, method, body }) => {
		const response = await window.apiFetch(endpoint, {
			method, headers: { "Content-Type": "application/json" },
			...(body === undefined ? {} : { body: JSON.stringify(body) }),
		});
		return { status: response.status, data: await response.json() };
	}, { endpoint, method, body });
	const ready = async (page) => {
		await page.waitForFunction(() => {
			const root = document.getElementById("dashboard-grid");
			return root?.gridstack && !root.classList.contains("dashboard-grid--loading") &&
				!document.getElementById("dashboard-edit-toggle")?.disabled;
		}, null, { timeout: 15000 });
		await waitForVisualStability(page);
	};
	const geometry = async (page) => {
		const snapshot = await page.evaluate(() => {
			const root = document.getElementById("dashboard-grid");
			const bounds = root.getBoundingClientRect();
			const widgets = Array.from(root.querySelectorAll('.grid-stack-item:not(.d-none)')).map((element) => {
				const content = element.querySelector(".grid-stack-item-content");
				const rect = content.getBoundingClientRect();
				const card = content.querySelector(".dashboard-widget-card");
				const cardRect = card && card.getBoundingClientRect();
				const handle = content.querySelector(":scope > .dashboard-widget-handle");
				const { x, y, w, h } = element.gridstackNode;
				return { id: element.dataset.widgetId, left: rect.left, top: rect.top, right: rect.right,
					bottom: rect.bottom, text: content.textContent.trim(), width: rect.width, height: rect.height,
					card: cardRect && { left: cardRect.left, top: cardRect.top, right: cardRect.right, bottom: cardRect.bottom,
						width: cardRect.width },
					handle: handle && { hidden: handle.hidden, height: handle.hidden ? 0 : handle.getBoundingClientRect().height },
					grid: { x, y, w, h } };
			});
			return { left: bounds.left, right: bounds.right, widgets,
				overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth };
		});
		assert.ok(snapshot.widgets.length > 0, "dashboard has no visible widgets");
		assert.ok(snapshot.overflow <= 2, `page overflows by ${snapshot.overflow}px`);
		for (const widget of snapshot.widgets) {
			assert.ok(widget.width > 0 && widget.height > 0 && widget.text, `empty widget ${widget.id}`);
			assert.ok(widget.left >= snapshot.left - 2 && widget.right <= snapshot.right + 2, `widget outside grid: ${widget.id}`);
			if (widget.card) {
				const expectedTop = widget.top;
				assert.ok(Math.abs(widget.card.left - widget.left) <= 2 && Math.abs(widget.card.right - widget.right) <= 2,
					`widget card does not fill its grid cell width: ${widget.id} ` + JSON.stringify({ cell: [widget.left, widget.right], card: widget.card }));
				assert.ok(Math.abs(widget.card.top - expectedTop) <= 2 && Math.abs(widget.card.bottom - widget.bottom) <= 2,
					`widget card does not fill its reserved grid cell: ${widget.id} ` + JSON.stringify({ cell: [widget.top, widget.bottom], handle: widget.handle, card: [widget.card.top, widget.card.bottom] }));
			}
			for (const other of snapshot.widgets) {
				if (other.id === widget.id) continue;
				const overlapX = Math.min(widget.right, other.right) - Math.max(widget.left, other.left);
				const overlapY = Math.min(widget.bottom, other.bottom) - Math.max(widget.top, other.top);
				assert.ok(overlapX <= 2 || overlapY <= 2, `widgets overlap: ${widget.id}, ${other.id} ` +
					JSON.stringify({ widget: { left: widget.left, top: widget.top, right: widget.right, bottom: widget.bottom, grid: widget.grid },
						other: { left: other.left, top: other.top, right: other.right, bottom: other.bottom, grid: other.grid } }));
			}
		}
	};
	// Edit-mode geometry contract: GridStack's node coordinates are the only authority. Frame, card,
	// the painted empty-cell slots and the drag placeholder must all land on the rectangle those
	// coordinates describe, and entering edit mode must not move or resize any frame.
	const gridSnapshot = (page) => page.evaluate(() => {
		const root = document.getElementById("dashboard-grid");
		const rb = root.getBoundingClientRect();
		const columns = root.gridstack.getColumn();
		const rect = (element) => {
			if (!element) return null;
			const { left, top, right, bottom } = element.getBoundingClientRect();
			return { left, top, right, bottom };
		};
		const visibleSlot = (slot) => {
			const box = slot.getBoundingClientRect();
			const style = getComputedStyle(slot);
			return { left: box.left + parseFloat(style.paddingLeft), top: box.top + parseFloat(style.paddingTop),
				right: box.right - parseFloat(style.paddingRight), bottom: box.bottom - parseFloat(style.paddingBottom) };
		};
		const placeholder = root.querySelector(":scope > .grid-stack-placeholder");
		return {
			root: { left: rb.left, top: rb.top, width: rb.width }, columns,
			cell: root.gridstack.getCellHeight(), margin: Number(root.gridstack.opts.margin),
			slots: [...root.querySelectorAll(".dashboard-slot")].map(visibleSlot),
			placeholder: placeholder && {
				rect: rect(placeholder.querySelector(".placeholder-content")),
				node: ["x", "y", "w", "h"].map((key) => Number(placeholder.getAttribute(`gs-${key}`))),
			},
			widgets: [...root.querySelectorAll(".grid-stack-item:not(.d-none):not(.grid-stack-placeholder)")].map((element) => {
				const frame = element.querySelector(".grid-stack-item-content");
				const handle = frame.querySelector(":scope > .dashboard-widget-handle");
				const handleRect = handle && !handle.hidden ? rect(handle) : null;
				const covered = handleRect ? [...frame.querySelectorAll("*")].filter((node) => !handle.contains(node) &&
					[...node.childNodes].some((child) => child.nodeType === 3 && child.textContent.trim())).map((node) => ({ node: node.className || node.tagName, ...rect(node) }))
					.filter((box) => box.right > box.left && box.bottom > box.top) : [];
				const { x, y, w, h } = element.gridstackNode;
				return { id: element.dataset.widgetId, node: { x, y, w, h }, frame: rect(frame),
					moving: element.classList.contains("ui-draggable-dragging") || element.classList.contains("ui-resizable-resizing"),
					card: rect(frame.querySelector(".dashboard-widget-card")), handle: handleRect, text: covered };
			}),
		};
	});
	const expectGridGeometry = async (page, label, { editing = false, against, dragging = false } = {}) => {
		// Slots follow the grid height through a ResizeObserver; let that frame run before measuring.
		await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
		await waitForVisualStability(page);
		const snap = await gridSnapshot(page);
		const tol = GEOMETRY_TOL_PX;
		const colWidth = snap.root.width / snap.columns;
		const edges = ["left", "top", "right", "bottom"];
		const near = (actual, expected, what) => {
			for (const edge of edges) {
				assert.ok(Math.abs(actual[edge] - expected[edge]) <= tol,
					`${label}: ${what} ${edge} is ${actual[edge].toFixed(2)}, expected ${expected[edge].toFixed(2)} (tolerance ${tol}px)`);
			}
		};
		const cellRect = ({ x, y, w, h }) => ({
			left: snap.root.left + x * colWidth + snap.margin, top: snap.root.top + y * snap.cell + snap.margin,
			right: snap.root.left + (x + w) * colWidth - snap.margin, bottom: snap.root.top + (y + h) * snap.cell - snap.margin,
		});
		const tile = (slotIndex) => snap.slots[slotIndex];
		for (const widget of snap.widgets.filter((entry) => !entry.moving)) {
			const expected = cellRect(widget.node);
			near(widget.frame, expected, `${widget.id} frame (grid ${JSON.stringify(widget.node)})`);
			if (widget.card) near(widget.card, widget.frame, `${widget.id} card vs frame`);
			if (editing) {
				const { x, y, w, h } = widget.node;
				const first = tile(y * snap.columns + x);
				const last = tile((y + h - 1) * snap.columns + x + w - 1);
				assert.ok(first && last, `${label}: no empty-cell slot covers ${widget.id} at ${JSON.stringify(widget.node)}`);
				near(widget.frame, { left: first.left, top: first.top, right: last.right, bottom: last.bottom }, `${widget.id} frame vs painted grid cells`);
				assert.ok(widget.handle, `${label}: ${widget.id} has no visible drag handle`);
				assert.ok(widget.handle.left >= widget.frame.left - tol && widget.handle.right <= widget.frame.right + tol &&
					Math.abs(widget.handle.top - widget.frame.top) <= tol && widget.handle.bottom <= widget.frame.bottom + tol,
					`${label}: ${widget.id} drag handle leaves its frame ${JSON.stringify({ handle: widget.handle, frame: widget.frame })}`);
				for (const text of widget.text) {
					assert.ok(text.top >= widget.handle.bottom - tol || text.bottom <= widget.handle.top + tol ||
						text.left >= widget.handle.right - tol || text.right <= widget.handle.left + tol,
						`${label}: drag handle overlaps text in ${widget.id} (${text.node}) ${JSON.stringify({ handle: widget.handle, text })}`);
				}
			} else {
				assert.equal(widget.handle, null, `${label}: ${widget.id} handle is visible outside edit mode`);
			}
		}
		if (against) {
			for (const widget of snap.widgets.filter((entry) => !entry.moving)) {
				const before = against.widgets.find((other) => other.id === widget.id);
				assert.ok(before, `${label}: ${widget.id} was not part of the reference snapshot`);
				near(widget.frame, before.frame, `${widget.id} frame moved between modes`);
			}
		}
		if (dragging) assert.ok(snap.placeholder && snap.placeholder.rect, `${label}: no drag placeholder is shown`);
		if (snap.placeholder && snap.placeholder.rect) {
			const [x, y, w, h] = snap.placeholder.node;
			near(snap.placeholder.rect, cellRect({ x, y, w, h }), "drag placeholder");
		}
		return snap;
	};
	// Corner resize grip: 24px target inside the frame, a local diagonal arrow from the shared rule, no text underneath.
	const expectResizeGrips = async (page, label) => {
		const grips = await page.evaluate(() => [...document.querySelectorAll("#dashboard-grid .grid-stack-item:not(.d-none) > .ui-resizable-se")].map((grip) => {
			const frame = grip.closest(".grid-stack-item").querySelector(".grid-stack-item-content").getBoundingClientRect();
			const box = grip.getBoundingClientRect();
			const text = [];
			const walker = document.createTreeWalker(grip.closest(".grid-stack-item"), NodeFilter.SHOW_TEXT);
			for (let node = walker.nextNode(); node; node = walker.nextNode()) {
				if (!node.textContent.trim() || node.parentElement.closest(".visually-hidden, .material-icons, [hidden]")) continue;
				const range = document.createRange();
				range.selectNodeContents(node);
				for (const rect of range.getClientRects()) {
					// Only what is painted counts: clip the line box by every clipping or scrolling ancestor.
					const visible = { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom };
					for (let parent = node.parentElement; parent && parent !== document.body; parent = parent.parentElement) {
						const style = getComputedStyle(parent);
						if (style.overflowX === "visible" && style.overflowY === "visible") continue;
						const clip = parent.getBoundingClientRect();
						visible.left = Math.max(visible.left, clip.left); visible.right = Math.min(visible.right, clip.right);
						visible.top = Math.max(visible.top, clip.top); visible.bottom = Math.min(visible.bottom, clip.bottom);
					}
					if (visible.right > visible.left && visible.bottom > visible.top) {
						text.push({ ...visible, label: `${node.parentElement.className}: ${node.textContent.trim().slice(0, 30)}` });
					}
				}
			}
			return { id: grip.closest(".grid-stack-item").dataset.widgetId, width: box.width, height: box.height,
				inside: box.left >= frame.left && box.top >= frame.top && box.right <= frame.right && box.bottom <= frame.bottom,
				overlapsText: text.filter((rect) => rect.left < box.right && rect.right > box.left && rect.top < box.bottom && rect.bottom > box.top).map((rect) => rect.label).join(' | '),
				cursor: getComputedStyle(grip).cursor, transform: getComputedStyle(grip).transform,
				mask: getComputedStyle(grip, "::after").maskImage, origin: location.origin };
		}));
		assert.ok(grips.length > 0, `${label}: no resize grips in edit mode`);
		for (const grip of grips) {
			assert.ok(grip.width >= 24 - GEOMETRY_TOL_PX && grip.height >= 24 - GEOMETRY_TOL_PX, `${label}: ${grip.id} resize target is ${grip.width}x${grip.height}`);
			assert.ok(grip.inside, `${label}: ${grip.id} resize grip leaves its frame`);
			assert.equal(grip.overlapsText, "", `${label}: ${grip.id} resize grip overlaps text`);
			assert.equal(grip.cursor, "nwse-resize", `${label}: ${grip.id} resize cursor`);
			assert.equal(grip.transform, "none", `${label}: ${grip.id} grip is rotated`);
			assert.equal(grip.mask, `url("${grip.origin}/static/img/icons/resize-se.svg")`, `${label}: ${grip.id} grip glyph is not the local shared SVG mask`);
		}
	};
	const widgetChrome = async (page, editing = false) => {
		const shells = await page.evaluate(() => ['downtime', 'alerts-open', 'delivery-queue']
			.map((id) => document.querySelector(`[data-widget-id="${id}"]`))
			.filter((tile) => tile && !tile.classList.contains('d-none'))
			.map((tile) => {
				const card = tile.querySelector('.dashboard-widget-card');
				const header = card?.querySelector('.dashboard-widget-header');
				const body = card?.querySelector('.dashboard-widget-body');
				const handle = tile.querySelector('.dashboard-widget-handle');
				const rect = (element) => {
					const { left, right, top, bottom } = element.getBoundingClientRect();
					return { left, right, top, bottom };
				};
				return {
					id: tile.dataset.widgetId,
					header: header && rect(header),
					body: body && rect(body),
					handle: handle && { ...rect(handle), hidden: handle.hidden },
					headerFont: header && getComputedStyle(header).fontSize,
					bodyFont: body && getComputedStyle(body).fontSize,
					spacing: getComputedStyle(document.getElementById('dashboard-grid')).getPropertyValue('--ju-dashboard-card-padding').trim(),
				};
			}));
		assert.ok(shells.some((shell) => shell.id === 'downtime'), 'downtime widget shell is missing');
		assert.ok(shells.some((shell) => shell.id === 'alerts-open'), 'alerts widget shell is missing');
		for (const shell of shells) {
			assert.ok(shell.header && shell.body, `${shell.id} has no shared header/body shell`);
			assert.ok(shell.header.bottom <= shell.body.top + 1, `${shell.id} header overlaps its body`);
			assert.equal(shell.headerFont, shells[0].headerFont, `${shell.id} title typography differs from the shared shell`);
			assert.equal(shell.bodyFont, shells[0].bodyFont, `${shell.id} body typography differs from the shared shell`);
			assert.equal(shell.spacing, shells[0].spacing, `${shell.id} card spacing token differs from the shared shell`);
			if (editing) {
				assert.ok(shell.handle && !shell.handle.hidden, `${shell.id} drag handle is not visible in edit mode`);
				assert.ok(shell.handle.bottom <= shell.header.top + 1, `${shell.id} drag handle overlaps its title`);
			}
		}
	};
	const node = (widget) => widget.evaluate((element) => {
		const { x, y, w, h } = element.gridstackNode;
		return { x, y, w, h };
	});
	const expectQueueContent = async (page, label) => {
		const geometry = await page.locator('[data-widget-id="delivery-queue"]').evaluate((tile) => {
			const content = tile.querySelector('.grid-stack-item-content');
			const inner = content.querySelector(':scope > :not(.dashboard-widget-handle)');
			const saved = ['position', 'width', 'height', 'flex', 'transition'].map((name) => [
				name, inner.style.getPropertyValue(name), inner.style.getPropertyPriority(name),
			]);
			const width = inner.getBoundingClientRect().width;
			inner.style.setProperty('transition', 'none', 'important');
			inner.style.setProperty('position', 'absolute', 'important');
			inner.style.setProperty('width', `${width}px`, 'important');
			inner.style.setProperty('height', 'auto', 'important');
			inner.style.setProperty('flex', '0 0 auto', 'important');
			const naturalHeight = Math.ceil(inner.getBoundingClientRect().height) + 1;
			for (const [name, value, priority] of saved) {
				if (value) inner.style.setProperty(name, value, priority);
				else inner.style.removeProperty(name);
			}
			const grid = tile.closest('.grid-stack');
			const frame = tile.offsetHeight - content.offsetHeight;
			const band = grid.classList.contains('dashboard-grid--editing') ? 0 :
				parseFloat(getComputedStyle(grid).getPropertyValue('--ju-dash-handle-height')) * parseFloat(getComputedStyle(document.documentElement).fontSize);
			const cellHeight = parseFloat(getComputedStyle(grid).getPropertyValue('--ju-dash-cell-height'));
			const header = tile.querySelector('.dashboard-widget-header');
			const title = tile.querySelector('.dashboard-monitor-title');
			const status = tile.querySelector('.dashboard-monitor-status');
			const body = tile.querySelector('.dashboard-monitor-body');
			const metrics = body.querySelector('.dashboard-monitor-metrics');
			const rect = (element) => {
				const { left, right, top, bottom } = element.getBoundingClientRect();
				return { left, right, top, bottom };
			};
			const overlaps = (left, right) => left.left < right.right && left.right > right.left && left.top < right.bottom && left.bottom > right.top;
			const bounds = rect(metrics);
			const metricBoxes = [...metrics.querySelectorAll('dt, dd')].map(rect);
			const bodyStyle = getComputedStyle(body);
			const containerPx = body.clientWidth - parseFloat(bodyStyle.paddingLeft) - parseFloat(bodyStyle.paddingRight);
			return { cols: tile.gridstackNode.w, minCols: tile.gridstackNode.minW,
				containerRem: containerPx / parseFloat(getComputedStyle(document.documentElement).fontSize),
				stacked: getComputedStyle(metrics).gridTemplateColumns.split(' ').length === 1,
				bodyScroll: [body.scrollHeight, body.clientHeight],
				rows: tile.gridstackNode.h, minRows: tile.gridstackNode.minH, naturalHeight, frame, band, cellHeight,
				requiredRows: Math.ceil((naturalHeight + frame + band) / cellHeight),
				headerOverflow: header.scrollWidth - header.clientWidth,
				bodyOverflow: body.scrollHeight - body.clientHeight, metricsOverflow: metrics.scrollWidth - metrics.clientWidth,
				titleStatusOverlap: overlaps(rect(title), rect(status)),
				metricOutside: metricBoxes.some((box) => box.left < bounds.left - 1 || box.right > bounds.right + 1 || box.top < bounds.top - 1 || box.bottom > bounds.bottom + 1) };
		});
		assert.equal(geometry.rows, 3, `${label}: Queue height differs from its measured three-row minimum ${JSON.stringify(geometry)}`);
		assert.equal(geometry.minRows, 3, `${label}: GridStack Queue minimum differs from its measured minimum ${JSON.stringify(geometry)}`);
		assert.ok(geometry.requiredRows <= 3, `${label}: Queue content requires more than three rows ${JSON.stringify(geometry)}`);
		assert.ok(geometry.headerOverflow <= 1, `${label}: Queue header overflows ${JSON.stringify(geometry)}`);
		assert.ok(geometry.bodyOverflow <= 1, `${label}: Queue body scrolls or clips content ${JSON.stringify(geometry)}`);
		assert.ok(geometry.metricsOverflow <= 1 && !geometry.metricOutside, `${label}: Queue metrics overflow their container ${JSON.stringify(geometry)}`);
		assert.equal(geometry.titleStatusOverlap, false, `${label}: Queue title overlaps its status badge ${JSON.stringify(geometry)}`);
		return geometry;
	};
	if (phase !== "legacy") {
		const setup = await browser.newContext({ storageState: adminState });
		try {
			const page = await setup.newPage();
			await page.goto(app.base + "/");
			assert.equal((await request(page, "/me/dashboard-layout", "DELETE")).status, 200);
			if (phase === "roles") assert.equal((await request(page, "/users", "POST", { username, password, is_admin: false })).status, 200);
		} finally {
			await setup.close();
		}
	}
	let adminLayout;
	if (phase === "roles") for (const role of ["admin", "viewer"]) {
		const context = await browser.newContext({ viewport: { width: 1440, height: 1000 },
			...(role === "admin" ? { storageState: adminState } : {}) });
		const page = await context.newPage();
		const errors = [];
		page.on("pageerror", (error) => errors.push(String(error)));
		try {
			let queuePayloadOverride = null;
			let queueRequestFailure = false;
			let qualityUnavailable = false;
			let qualityPayload = {
				score: 94.9, state: "ok", updated_at: "2026-11-02T12:00:00Z",
				targets: ["https://1.1.1.1", "8.8.8.8", "9.9.9.9", "https://www.de-cix.net", "https://cloudflare.com"],
				target_details: [
					{ target: "https://1.1.1.1", country_code: "DE" },
					{ target: "8.8.8.8", country_code: null },
					{ target: "9.9.9.9", country_code: null },
					{ target: "https://www.de-cix.net", country_code: "US" },
					{ target: "https://cloudflare.com", country_code: null },
				],
			};
			const initialQualityPayload = qualityPayload;
			if (role === "admin") {
				await page.route("**/quality/current", (route) => route.fulfill({ status: qualityUnavailable ? 503 : 200,
					contentType: "application/json", body: JSON.stringify(qualityPayload) }));
			}
			if (role === "viewer") {
				await page.goto(app.base + "/ui/login");
				await page.getByLabel("Username").fill(username);
				await page.getByLabel("Password", { exact: true }).fill(password);
				await page.locator('#login-form button[type="submit"]').click();
				await page.waitForURL((url) => !/login|change-password/.test(url.pathname), { timeout: 10000 });
			} else await page.goto(app.base + "/");
			assert.equal((await request(page, "/me/dashboard-layout")).data.layout, null, "users must not inherit another user's layout");
			await ready(page);
			if (role === "admin") {
				const quality = page.locator('[data-widget-id="quality"]');
				await quality.locator('.quality-target').first().waitFor({ state: "visible" });
				// Gap between subtitle and badges versus badges and the end of the widget content.
				const qualitySpacing = () => quality.evaluate((element) => {
					const label = element.querySelector('#quality-label').getBoundingClientRect();
					const rows = [...element.querySelectorAll('.quality-target:not([hidden])')].map((badge) => badge.getBoundingClientRect());
					const body = element.querySelector('.quality-body').getBoundingClientRect();
					return { above: Math.min(...rows.map((r) => r.top)) - label.bottom, below: body.bottom - parseFloat(getComputedStyle(element.querySelector('.quality-body')).paddingBottom) - Math.max(...rows.map((r) => r.bottom)) };
				});
				const expectQualityCentred = async (label) => {
					await waitForVisualStability(page);
					const empty = await quality.evaluate((element) => {
						if (element.querySelector('.quality-target:not([hidden])')) return null;
						const probe = document.body.appendChild(Object.assign(document.createElement('span'), { className: 'badge quality-target', textContent: '+9' }));
						const need = probe.getBoundingClientRect().height;
						probe.remove();
						return { zone: element.querySelector('.quality-targets').clientHeight, need };
					});
					if (empty) {
						// Without a visible badge the zone must be physically too small for one.
						assert.ok(empty.zone < empty.need, `${label}: quality badges hidden although they fit ${JSON.stringify(empty)}`);
						return;
					}
					const spacing = await qualitySpacing();
					// At the registry minimum the free space is physically small; it must stay equal on both sides and non-negative.
					assert.ok(spacing.above >= 1, `${label}: quality badges touch the subtitle ${JSON.stringify(spacing)}`);
					assert.ok(Math.abs(spacing.above - spacing.below) <= 2, `${label}: quality badges are not vertically centred ${JSON.stringify(spacing)}`);
					const badgeFindings = (await runAudit(page, 'top')).issues.filter((finding) => finding.type.startsWith('badge-'));
					assert.deepEqual(badgeFindings, [], `${label}: badge audit findings`);
				};
				assert.ok(await quality.evaluate((element) => element.gridstackNode.h >= 2),
					'quality tile fell below its minimum height');
				await expectQualityCentred('default layout');
				await page.locator('#delivery-queue-status').getByText('Queue clear').waitFor();
				const queueBody = page.locator('[data-widget-id="delivery-queue"] .dashboard-monitor-body');
				const queueCells = () => queueBody.locator('.dashboard-monitor-metrics > div');
				await page.route("**/settings/notification-queue/stats", async (route) => {
					if (queueRequestFailure) return route.fulfill({ status: 503, contentType: "application/json", body: "{}" });
					if (queuePayloadOverride) return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(queuePayloadOverride) });
					return route.continue();
				});
				const reloadQueue = async (values) => {
					const response = page.waitForResponse((result) => result.url().endsWith('/settings/notification-queue/stats'));
					await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
					await response;
					if (values) await page.waitForFunction((expected) =>
						[...document.querySelectorAll('[data-widget-id="delivery-queue"] .dashboard-monitor-metrics dd')]
							.map((value) => value.textContent.trim()).join(',') === expected.join(','), values);
				};
				queuePayloadOverride = { pending: 123456789, processing: 234567890, failed: 987654321, failed_recent: null,
					stuck_processing: 0, processing_timeout_seconds: 300, oldest_pending: new Date().toISOString(),
					next_retry_at: null, services: { monitor_scheduler: { state: 'running' } } };
				await reloadQueue(['123456789', '234567890', '987654321']);
				assert.deepEqual(await queueCells().locator('dd').allTextContents(), ['123456789', '234567890', '987654321'],
					'large Queue values or the failed-count fallback were not rendered');
				queuePayloadOverride = { pending: 0, processing: 0, failed: 0, failed_recent: 0, stuck_processing: 0,
					processing_timeout_seconds: 300, oldest_pending: null, next_retry_at: null,
					services: { monitor_scheduler: { state: 'running' } } };
				await reloadQueue(['0', '0', '0']);
				assert.deepEqual(await queueCells().locator('dd').allTextContents(), ['0', '0', '0'], 'zero Queue counts are not visible');
				queuePayloadOverride = null;
				queueRequestFailure = true;
				await reloadQueue();
				await page.locator('#delivery-queue-status').getByText('Status unavailable').waitFor();
				assert.equal(await queueBody.locator('.dashboard-monitor-metrics').count(), 0, 'failed Queue refresh retained stale metrics');
				assert.ok((await queueBody.innerText()).includes('Delivery queue metrics unavailable'), 'failed Queue refresh has no visible explanation');
				queueRequestFailure = false;
				await reloadQueue();
				await page.locator('#delivery-queue-status').getByText('Queue clear').waitFor();
				assert.equal(await queueCells().count(), 3, 'Queue metrics did not recover after an API failure');
				for (const theme of ['light', 'dark']) {
					const colors = await page.evaluate((value) => {
					document.documentElement.setAttribute('data-bs-theme', value);
					return ['quality-state-badge', 'delivery-queue-status'].map((id) => {
						const badge = document.getElementById(id);
						const style = getComputedStyle(badge);
						return { background: style.backgroundColor, foreground: style.color,
							variant: [...badge.classList].find((name) => name.startsWith('ju-status-badge--')) };
						});
					}, theme);
					assert.deepEqual(colors[0], colors[1], `${theme}: healthy dashboard badges use different colors`);
				assert.equal(colors[0].variant, 'ju-status-badge--success', `${theme}: healthy dashboard badges lack the shared success variant`);
				}
				await page.evaluate(() => document.documentElement.setAttribute('data-bs-theme', 'light'));
				assert.equal(
					await quality.locator('.card-header').evaluate((header) => header.getBoundingClientRect().height),
					await page.locator('[data-widget-id="delivery-queue"] .card-header').evaluate((header) => header.getBoundingClientRect().height),
					'quality heading should match the delivery queue header height',
				);
				assert.equal(await quality.locator('#quality-updated').count(), 0, 'quality measurement time should not be shown');
				assert.equal(await quality.locator('.quality-widget-icon').count(), 1, 'quality title should use the SVG icon');
				await page.waitForFunction(() => {
					const value = document.querySelector('#dashboard-status-online .dashboard-widget-value');
					return value && value.textContent.trim() !== '–';
				}, null, { timeout: 10000 });
				assert.equal(
					await quality.locator('#quality-score').evaluate((score) => getComputedStyle(score).fontSize),
					await page.locator('#dashboard-status-online .dashboard-widget-value').evaluate((hero) => getComputedStyle(hero).fontSize),
					'quality score should match dashboard hero numbers',
				);
				assert.equal(await page.locator('#quality-targets-tooltip-trigger, .quality-targets-tooltip').count(), 0);
				assert.equal(await quality.locator('.quality-target').first().textContent(), '1.1.1.1');
				assert.equal(await quality.locator('.quality-target').first().getAttribute('aria-label'), 'DE: 1.1.1.1');
				assert.equal(await quality.locator('.quality-target').first().locator('img').getAttribute('src'),
					'https://cdn.jsdelivr.net/npm/flag-icons@7.3.2/flags/4x3/de.svg');
				assert.equal(await quality.locator('.quality-target').nth(1).textContent(), '8.8.8.8');
				assert.match(await quality.locator('.quality-target').nth(1).locator('.quality-target-flag').evaluate((icon) => getComputedStyle(icon).maskImage),
					/\/static\/img\/icons\/flag\.svg/);
				assert.equal(await quality.locator('.quality-target-flag').count(), 5, 'every target should have a country or generic SVG flag');
				const v2Layout = await page.evaluate(() => ({
					version: 2,
					widgets: window.JuDashboardRegistry.forRole(true).map((widget) => ({
						id: widget.id, x: widget.defaultX, y: widget.defaultY, w: widget.defaultSize.w,
						h: widget.id === 'quality' ? 3 : widget.defaultSize.h,
						visible: widget.defaultVisible, height_mode: 'auto',
					})),
				}));
				const persisted = await request(page, '/me/dashboard-layout', 'PUT', v2Layout);
				assert.equal(persisted.status, 200);
				assert.deepEqual(persisted.data.layout, v2Layout, 'V2 layout was not persisted exactly');
				assert.deepEqual((await request(page, '/me/dashboard-layout')).data.layout, v2Layout,
					'V2 layout did not reload from storage');
				await page.reload();
				await ready(page);
				assert.ok(await quality.evaluate((element) => element.gridstackNode.h >= 2),
					'V2 automatic quality height fell below its minimum');
				const before = await node(quality);
				const visible = () => quality.locator('.quality-target:not([hidden]):not(.quality-target-overflow)').count();
				await quality.evaluate((element) => element.closest('.grid-stack').gridstack.update(element, { w: 6, h: 5 }));
				await page.waitForFunction(() => document.querySelectorAll('[data-widget-id="quality"] .quality-target:not([hidden]):not(.quality-target-overflow)').length === 5);
				assert.equal(await visible(), 5, 'large tile should show all targets');
				assert.equal(await quality.locator('.quality-target-overflow').isVisible(), false);
				await quality.evaluate((element) => element.closest('.grid-stack').gridstack.update(element, { w: 2, h: 2 }));
				await page.waitForFunction(() => {
					const targets = document.querySelectorAll('[data-widget-id="quality"] .quality-target:not([hidden]):not(.quality-target-overflow)').length;
					const counter = document.querySelector('[data-widget-id="quality"] .quality-target-overflow:not([hidden])');
					return targets === 5 || counter && counter.textContent === `+${5 - targets}` && targets < 5;
				});
				const narrowFit = await quality.evaluate((element) => ({
					hidden: 5 - element.querySelectorAll('.quality-target:not([hidden]):not(.quality-target-overflow)').length,
					counter: element.querySelector('.quality-target-overflow').textContent,
					counterVisible: !element.querySelector('.quality-target-overflow').hidden,
				}));
				const narrowHidden = narrowFit.hidden;
				assert.equal(narrowFit.counterVisible, narrowHidden > 0);
				if (narrowHidden > 0) assert.equal(narrowFit.counter, '+' + narrowHidden);
				await quality.evaluate((element, size) => element.closest('.grid-stack').gridstack.update(element, size), before);
				await waitForVisualStability(page);
				const savedQuality = (await request(page, '/me/dashboard-layout')).data.layout.widgets.find((widget) => widget.id === 'quality');
				assert.equal(savedQuality.h, persisted.data.layout.widgets.find((widget) => widget.id === 'quality').h,
					'content fit changed the saved quality height');
				assert.equal(savedQuality.height_mode, 'auto', 'content fit persisted as a manual choice');
				const hidden = 5 - await visible();
				assert.equal(await quality.locator('.quality-target-overflow').isVisible(), hidden > 0);
				if (hidden) assert.equal(await quality.locator('.quality-target-overflow').textContent(), '+' + hidden);
				const targetGeometry = await quality.locator('#quality-targets').evaluate((container) => {
					const bounds = container.getBoundingClientRect();
					return [...container.children].filter((child) => !child.hidden).map((child) => {
						const box = child.getBoundingClientRect();
						return box.left >= bounds.left - 1 && box.right <= bounds.right + 1 && box.bottom <= bounds.bottom + 1;
					});
				});
				assert.ok(targetGeometry.every(Boolean), 'quality badges overflow their container');
				const spacing = await qualitySpacing();
				assert.ok(spacing.above >= 4, 'quality badges touch the subtitle');
				assert.ok(Math.abs(spacing.above - spacing.below) <= 2, 'quality badges are not vertically centred below the subtitle');
				// Stored heights and widths: default height, minimum size, manual enlargement and a narrow tile, each from a fresh load.
				const applyQuality = async (size) => {
					const layout = { ...v2Layout, widgets: v2Layout.widgets.map((widget) => widget.id === 'quality' ? { ...widget, ...size } : widget) };
					assert.equal((await request(page, '/me/dashboard-layout', 'PUT', layout)).status, 200);
					await page.reload();
					await ready(page);
					await quality.locator('.quality-target').first().waitFor({ state: 'attached' });
				};
				for (const [label, size] of [['registry default height', { w: 3, h: 3, height_mode: 'auto' }], ['minimum size', { w: 2, h: 3, height_mode: 'manual' }],
					['manual enlarged', { w: 3, h: 5, height_mode: 'manual' }], ['narrow width', { w: 2, h: 4, height_mode: 'manual' }]]) {
					await applyQuality(size);
					await expectQualityCentred(label);
				}
				await applyQuality({ w: v2Layout.widgets.find((widget) => widget.id === 'quality').w, h: 3, height_mode: 'auto' });
				await quality.locator('.quality-target').first().evaluate((element) => { window.__qualityBadge = element; });
				const refresh = page.waitForResponse((response) => response.url().endsWith('/quality/current'));
				await page.evaluate(() => document.body.dispatchEvent(new Event('dashboard-refresh')));
				await refresh;
				assert.equal(await quality.locator('.quality-target').first().evaluate((element) => element === window.__qualityBadge), true,
					'unchanged targets were reconstructed');
				qualityUnavailable = true;
				for (let attempt = 0; attempt < 2; attempt++) {
					const failed = page.waitForResponse((response) => response.url().endsWith('/quality/current'));
					await page.evaluate(() => document.body.dispatchEvent(new Event('dashboard-refresh')));
					await failed;
				}
				await page.waitForFunction(() => document.getElementById('quality-label')?.textContent === 'Data unavailable');
				assert.equal(await quality.locator('.quality-target').count(), 6, 'API failure lost known targets');
				qualityUnavailable = false;
				qualityPayload = { ...qualityPayload,
					targets: ["https://[2001:4860:4860::8888]:8443/check", "very-long-quality-hostname-with-many-labels.example.org", "example.org"],
					target_details: [],
				};
				const changed = page.waitForResponse((response) => response.url().endsWith('/quality/current'));
				await page.evaluate(() => document.body.dispatchEvent(new Event('dashboard-refresh')));
				await changed;
				await page.waitForFunction(() => document.querySelectorAll('#quality-targets .quality-target:not(.quality-target-overflow)').length === 3);
				assert.equal(await quality.locator('.quality-target').first().textContent(), '[2001:4860:4860::8888]:8443/check');
				assert.equal(await quality.locator('.quality-target').nth(1).textContent(), 'very-long-quality-hostname-with-many-labels.example.org');
				qualityPayload = { ...qualityPayload, targets: [], target_details: [] };
				const emptied = page.waitForResponse((response) => response.url().endsWith('/quality/current'));
				await page.evaluate(() => document.body.dispatchEvent(new Event('dashboard-refresh')));
				await emptied;
				await page.waitForFunction(() => document.getElementById('quality-targets')?.textContent === 'No quality targets configured');
				assert.equal(await quality.locator('.quality-target').count(), 0);
				qualityPayload = initialQualityPayload;
				const restored = page.waitForResponse((response) => response.url().endsWith('/quality/current'));
				await page.evaluate(() => document.body.dispatchEvent(new Event('dashboard-refresh')));
				await restored;
				await page.waitForFunction(() => document.querySelectorAll('#quality-targets .quality-target:not(.quality-target-overflow)').length === 5);
			}
			assert.equal(await page.locator('[data-widget-id="delivery-queue"]').count(), role === "admin" ? 1 : 0);
			await geometry(page);
			await widgetChrome(page);
			const viewSnapshot = await expectGridGeometry(page, `${browserName}/${role} before edit mode`);
			await page.getByRole("button", { name: "Edit dashboard", exact: true }).click();
			await waitForVisualStability(page);
			await expectGridGeometry(page, `${browserName}/${role} right after entering edit mode`, { editing: true, against: viewSnapshot });
			await widgetChrome(page, true);
			await expectResizeGrips(page, `${browserName}/${role} edit mode`);
			const online = page.locator('[data-widget-id="status-online"]');
			const handle = online.getByRole("button", { name: /^Move or resize widget: Online\./ });
			const original = await node(online);
			await handle.press("ArrowDown");
			assert.ok((await node(online)).y > original.y, "keyboard movement did not move the widget");
			await handle.press("Shift+ArrowRight");
			assert.ok((await node(online)).w > original.w, "keyboard resize did not resize the widget");
			const quality = page.locator('[data-widget-id="quality"]');
			const qualityHandle = quality.getByRole("button", { name: /^Move or resize widget: Monitor quality\./ });
			const qualityBeforeResize = await node(quality);
			await qualityHandle.press("Shift+ArrowDown");
			assert.ok((await node(quality)).h > qualityBeforeResize.h, "keyboard height resize did not resize the widget");
			const downtime = page.locator('[data-widget-id="downtime"]');
			const downtimeBeforeResize = await node(downtime);
			const downtimeHandle = downtime.getByRole("button", { name: /^Move or resize widget: Downtime overview\./ });
			await downtimeHandle.press("Shift+ArrowDown");
			assert.ok((await node(downtime)).h > downtimeBeforeResize.h, "downtime keyboard height resize did not resize the widget");
			await waitForVisualStability(page);
			await expectGridGeometry(page, `${browserName}/${role} after keyboard move and resize`, { editing: true });
			await handle.scrollIntoViewIfNeeded();
			const beforeDrag = await node(online);
			const box = await handle.boundingBox();
			const cell = await page.locator("#dashboard-grid").evaluate((root) => ({
				width: root.clientWidth / root.gridstack.getColumn(), height: root.gridstack.getCellHeight(),
			}));
			await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
			await page.mouse.down();
			await page.mouse.move(box.x + box.width / 2 + cell.width, box.y + box.height / 2 + cell.height * 3, { steps: 12 });
			await expectGridGeometry(page, `${browserName}/${role} while dragging`, { editing: true, dragging: true });
			await page.mouse.up();
			assert.notDeepEqual(await node(online), beforeDrag, "mouse drag did not move the widget");
			await waitForVisualStability(page);
			await expectGridGeometry(page, `${browserName}/${role} after drag`, { editing: true });
			await page.getByRole("button", { name: "Edit widgets", exact: true }).click();
			const modal = page.locator("#dashboard-widgets-modal");
			await modal.getByRole("button", { name: "Remove widget: Disabled", exact: true }).click();
			if (role === "viewer") assert.equal(await modal.getByRole("button", { name: /widget: Delivery queue/ }).count(), 0);
			await modal.getByRole("button", { name: "Close", exact: true }).last().click();
			await modal.waitFor({ state: "hidden" });
			const savedResponse = page.waitForResponse((response) => response.url().endsWith("/me/dashboard-layout") && response.request().method() === "PUT");
			await page.getByRole("button", { name: "Done", exact: true }).click();
			const saveResult = await savedResponse;
			assert.ok(saveResult.ok(), `saving the dashboard failed: ${saveResult.status()} ${await saveResult.text()}`);
			await ready(page);
			await expectGridGeometry(page, `${browserName}/${role} after saving`);
			const saved = (await request(page, "/me/dashboard-layout")).data.layout;
			assert.equal(saved.widgets.find((widget) => widget.id === "status-disabled").visible, false);
			assert.equal(saved.widgets.find((widget) => widget.id === "status-disabled").height_mode, "auto");
			assert.equal(saved.version, 2);
			assert.equal(saved.widgets.find((widget) => widget.id === "status-online").height_mode, "auto",
				"width-only resize changed automatic height mode");
			assert.equal(saved.widgets.find((widget) => widget.id === "quality").height_mode, "manual",
				"keyboard height choice was not persisted as manual");
			assert.equal(saved.widgets.find((widget) => widget.id === "downtime").height_mode, "manual",
				"downtime height choice was not persisted as manual");
			await page.reload();
			await ready(page);
			assert.deepEqual((await request(page, "/me/dashboard-layout")).data.layout, saved, "reload changed the saved layout");
			await expectGridGeometry(page, `${browserName}/${role} after reload`);
			assert.equal(await page.locator('[data-widget-id="status-disabled"]').isVisible(), false);
			const storedOnline = saved.widgets.find((widget) => widget.id === "status-online");
			const restoredOnline = await node(online);
			assert.equal((await node(quality)).h, saved.widgets.find((widget) => widget.id === "quality").h,
				`reload did not restore the manually selected quality height (${JSON.stringify(await node(quality))} vs ${JSON.stringify(saved.widgets.find((widget) => widget.id === "quality"))})`);
			assert.equal((await node(downtime)).h, saved.widgets.find((widget) => widget.id === "downtime").h,
				`reload did not restore the manually selected downtime height (${JSON.stringify(await node(downtime))} vs ${JSON.stringify(saved.widgets.find((widget) => widget.id === "downtime"))})`);
			for (const dimension of ["x", "y", "w"]) {
				assert.equal(restoredOnline[dimension], storedOnline[dimension], `reload did not restore widget ${dimension}`);
			}
			for (const viewport of [{ width: 768, height: 1024 }, { width: 390, height: 844 }, { width: 1440, height: 1000 }]) {
				await page.setViewportSize(viewport);
				if (viewport.width === 390) await page.reload();
				await ready(page);
				if (role === "admin" && viewport.width === 390) {
					const tableViewport = page.locator("#downtime-table-container");
					await tableViewport.locator("tbody tr").first().waitFor({ state: "visible" });
					const tableScroll = await tableViewport.evaluate((element) => ({
						role: element.getAttribute("role"), label: element.getAttribute("aria-label"),
						tabIndex: element.tabIndex, overflowX: getComputedStyle(element).overflowX,
						scrollbarWidth: getComputedStyle(element).scrollbarWidth,
						scrollWidth: element.scrollWidth, clientWidth: element.clientWidth,
						cardLayout: getComputedStyle(element.querySelector("tbody tr")).display === "block",
						headerExposed: getComputedStyle(element.querySelector("thead")).display !== "none",
						labeledCells: [...element.querySelectorAll("tbody td")].every((cell) => cell.hasAttribute("data-label")),
						associatedCells: [...element.querySelectorAll("tbody td")].every((cell) => {
							const heading = document.getElementById(cell.headers);
							return heading?.tagName === "TH" && heading.scope === "col"
								&& heading.closest("table") === cell.closest("table")
								&& heading.textContent.trim() === cell.dataset.label;
						}),
					}));
					assert.equal(tableScroll.role, "region", "downtime table scroll area has no region role");
					assert.ok(tableScroll.label, "downtime table scroll area has no accessible name");
					assert.equal(tableScroll.tabIndex, 0, "downtime table scroll area is not keyboard focusable");
					assert.equal(tableScroll.overflowX, "auto", "downtime table has no local horizontal scroll container");
					assert.notEqual(tableScroll.scrollbarWidth, "none", "downtime table scrollbar is hidden");
					assert.ok(tableScroll.cardLayout, "downtime table did not reflow to labeled rows on mobile");
					assert.ok(tableScroll.headerExposed, "mobile downtime headings are removed from the accessibility tree");
					assert.ok(tableScroll.labeledCells, "mobile downtime table cells are missing visible row labels");
					assert.ok(tableScroll.associatedCells, "mobile downtime cells lack semantic column-heading associations");
					const accessibilityTree = await tableViewport.locator("table").ariaSnapshot();
					for (const heading of ["Target", "HTTP", "Ping", "TCP"]) {
						assert.match(accessibilityTree, new RegExp(`columnheader "${heading}"`),
							`mobile downtime column heading ${heading} is missing from the accessibility tree`);
					}
					assert.ok(tableScroll.scrollWidth <= tableScroll.clientWidth + 2,
						"responsive downtime table unexpectedly requires horizontal scrolling on mobile");
				}
				await geometry(page);
				if (role === "admin" && viewport.width < 992) {
					assert.equal(await page.locator('[data-widget-id="quality"] .quality-target:not([hidden]):not(.quality-target-overflow)').count(),
						5, `stacked quality tile hid targets at ${viewport.width}px`);
				}
				assert.deepEqual((await request(page, "/me/dashboard-layout")).data.layout, saved, "responsive reflow overwrote the desktop layout");
			}
			if (role === "viewer") {
				const denied = await request(page, "/me/dashboard-layout", "PUT", { version: 2,
					widgets: [{ id: "delivery-queue", x: 0, y: 0, w: 4, h: 3, visible: true, height_mode: "auto" }] });
				assert.equal(denied.status, 403, "viewer can persist the protected administrator widget");
				assert.deepEqual((await request(page, "/me/dashboard-layout")).data.layout, saved);
			}
			// Edit mode exists from the desktop breakpoint up. Below it the saved layout only reflows, and the
			// hidden edit controls and handles must not disturb it. 1280x720 at 200 % zoom is 640x360 CSS px.
			for (const viewport of [{ width: 1280, height: 720 }, { width: 640, height: 360 }, { width: 390, height: 844 }, { width: 320, height: 640 }]) {
				await page.setViewportSize(viewport);
				await ready(page);
				const label = `${browserName}/${role} ${viewport.width}x${viewport.height}`;
				if (role === "admin") {
					await waitForVisualStability(page);
					await expectQueueContent(page, label);
				}
				if (viewport.width >= 992) {
					const reference = await expectGridGeometry(page, `${label} view`);
					await page.getByRole("button", { name: "Edit dashboard", exact: true }).click();
					await waitForVisualStability(page);
					if (role === "admin") await expectQueueContent(page, `${label} edit`);
					await expectGridGeometry(page, `${label} edit`, { editing: true, against: reference });
					await page.keyboard.press("Escape");
					await waitForVisualStability(page);
					await expectGridGeometry(page, `${label} after leaving edit mode`, { against: reference });
				} else {
					const hiddenEdit = await page.evaluate(() => ({
						toolbar: getComputedStyle(document.getElementById("dashboard-edit-toolbar")).display,
						handles: [...document.querySelectorAll(".dashboard-widget-handle")].every((handle) => handle.hidden || getComputedStyle(handle).display === "none"),
						overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
					}));
					assert.equal(hiddenEdit.toolbar, "none", `${label}: edit controls are visible without edit support`);
					assert.ok(hiddenEdit.handles, `${label}: drag handles are visible without edit support`);
					assert.ok(hiddenEdit.overflow <= 2, `${label}: hidden edit controls cause overflow ${hiddenEdit.overflow}px`);
					await geometry(page);
				}
			}
			await page.setViewportSize({ width: 1440, height: 1000 });
			await ready(page);
			await page.getByRole("button", { name: "Edit dashboard", exact: true }).click();
			// Ghost scrolling: edit gestures must neither scroll the document nor let the scroll height follow
			// the pointer. The grid may extend by EDIT_RESERVE_ROWS (DESIGN.md) below the settled layout, no further.
			const EDIT_RESERVE_ROWS = 4;
			const scrollState = () => page.evaluate(() => {
				const scroller = document.scrollingElement;
				const main = document.getElementById("main-content");
				const grid = document.getElementById("dashboard-grid").gridstack;
				return { docTop: scroller.scrollTop, docOverflow: scroller.scrollHeight - scroller.clientHeight,
					mainOverflow: main.scrollHeight - main.clientHeight, rows: grid.getRow(), cell: grid.getCellHeight() };
			});
			const expectContained = async (what, settled) => {
				const state = await scrollState();
				assert.equal(state.docTop, 0, `${browserName}/${role} ${what}: the document scrolled`);
				assert.ok(state.docOverflow <= 1, `${browserName}/${role} ${what}: the document gained a scroll range (${state.docOverflow}px)`);
				assert.ok(state.rows <= settled.rows + EDIT_RESERVE_ROWS,
					`${browserName}/${role} ${what}: the grid grew to ${state.rows} rows, settled layout has ${settled.rows} (+${EDIT_RESERVE_ROWS} allowed)`);
				assert.ok(state.mainOverflow <= settled.mainOverflow + EDIT_RESERVE_ROWS * state.cell + 1,
					`${browserName}/${role} ${what}: main scroll range grew from ${settled.mainOverflow}px to ${state.mainOverflow}px`);
			};
			const settledState = await scrollState();
			const dragBox = await handle.boundingBox();
			const viewportHeight = page.viewportSize().height;
			await page.mouse.move(dragBox.x + dragBox.width / 2, dragBox.y + dragBox.height / 2);
			await page.mouse.down();
			await page.mouse.move(dragBox.x + 30, viewportHeight - 4, { steps: 20 });
			await page.mouse.wheel(0, 600);
			await page.waitForTimeout(400);
			await expectContained("while dragging to the bottom edge", settledState);
			await page.mouse.up();
			await waitForVisualStability(page);
			await expectContained("after the drop", settledState);
			const droppedState = await scrollState(); // the dropped widget is part of the settled layout now
			const resizeGrip = await page.locator('[data-widget-id="downtime"] .ui-resizable-se').boundingBox();
			await page.mouse.move(resizeGrip.x + 10, resizeGrip.y + 10);
			await page.mouse.down();
			await page.mouse.move(resizeGrip.x + 10, viewportHeight - 4, { steps: 20 });
			await page.mouse.wheel(0, 600);
			await page.waitForTimeout(400);
			await expectContained("while resizing to the bottom edge", droppedState);
			await page.mouse.up();
			await waitForVisualStability(page);
			await expectContained("after the resize", droppedState);
			await page.keyboard.press("Escape");
			await waitForVisualStability(page);
			assert.ok((await scrollState()).mainOverflow <= settledState.mainOverflow + 1, `${browserName}/${role}: leaving edit mode did not restore the scroll range`);
			await page.getByRole("button", { name: "Edit dashboard", exact: true }).click();
			const beforeCancel = await node(online);
			await handle.press("ArrowDown");
			await page.keyboard.press("Escape");
			assert.deepEqual((await request(page, "/me/dashboard-layout")).data.layout, saved, "cancel persisted unsaved changes");
			assert.deepEqual(await node(online), beforeCancel, "cancel did not restore the visible widget position");
			await waitForVisualStability(page);
			await expectGridGeometry(page, `${browserName}/${role} after cancelling edits`);
			await page.getByRole("button", { name: "Edit dashboard", exact: true }).click();
			await page.getByRole("button", { name: "Reset layout", exact: true }).click();
			const resetResponse = page.waitForResponse((response) => response.url().endsWith("/me/dashboard-layout") && response.request().method() === "PUT");
			await page.getByRole("button", { name: "Done", exact: true }).click();
			assert.ok((await resetResponse).ok());
			await page.reload();
			await ready(page);
			assert.equal(await page.locator('[data-widget-id="status-disabled"]').isVisible(), true);
			await geometry(page);
			await expectGridGeometry(page, `${browserName}/${role} after layout reset`);
			if (role === "admin") {
				adminLayout = (await request(page, "/me/dashboard-layout")).data.layout;
				await page.locator('[data-widget-id="delivery-queue"]').evaluate((tile) => {
					tile.classList.remove("d-none");
					document.dispatchEvent(new Event("dashboard:widget-shown"));
				});
				const queueBody = page.locator('[data-widget-id="delivery-queue"] .dashboard-monitor-body');
				await page.waitForFunction(() => {
					const tile = document.querySelector('[data-widget-id="delivery-queue"]');
					return tile?.querySelector('.dashboard-monitor-status')?.textContent.includes("Queue clear") &&
						tile.querySelector('.dashboard-monitor-body')?.textContent.includes("Failed (24 h)");
				}, null, { timeout: 10000 });
				// Presentation of the alert counters is checked against a fixed summary, not the live database.
				const alertSummaryMock = { open: 3, acknowledged_24h: 2, started_24h: 5, resolved_24h: 4 };
				await page.route("**/alerts/summary", (route) => route.fulfill({ json: alertSummaryMock }));
				await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
				await page.waitForFunction(() => document.querySelector('[data-widget-id="alerts-open"] .dashboard-monitor-metrics'), null, { timeout: 10000 });
				for (const label of ["Pending", "Processing"]) {
					assert.ok((await queueBody.innerText()).includes(label), `delivery queue is missing visible label: ${label}`);
				}
				assert.ok(!(await queueBody.innerText()).includes("Oldest pending age"));
				assert.ok(!(await queueBody.innerText()).includes("Next retry in"));
				// Alerts uses the delivery queue structure: header status, then three equal values above their labels.
				for (const id of ["delivery-queue", "alerts-open"]) {
					const structure = await page.locator(`[data-widget-id="${id}"]`).evaluate((tile) => {
						return { title: tile.querySelector(".dashboard-monitor-title").textContent.trim(),
							status: tile.querySelector(".dashboard-monitor-status").textContent.trim(),
							icons: tile.querySelectorAll(".dashboard-monitor-status .material-icons").length,
							mainCount: tile.querySelectorAll(".dashboard-monitor-number").length,
							statusCount: tile.querySelectorAll(".dashboard-monitor-status").length,
							metricsTag: tile.querySelector(".dashboard-monitor-metrics").tagName,
							cells: [...tile.querySelectorAll(".dashboard-monitor-metrics > div")].map((cell) => ({
								label: cell.querySelector("dt")?.textContent.trim(),
								value: cell.querySelector("dd")?.textContent.trim(),
								semanticPair: cell.firstElementChild.tagName === "DT" && cell.lastElementChild.tagName === "DD",
								valueAboveLabel: cell.querySelector("dd").getBoundingClientRect().bottom <= cell.querySelector("dt").getBoundingClientRect().top + 1 })) };
					});
					assert.ok(structure.status && structure.icons === 0, `${id} needs a header status with text and without an icon`);
					assert.equal(structure.statusCount, 1, `${id} status must appear only in the header`);
					assert.equal(structure.metricsTag, "DL", `${id} metrics are not a definition list`);
					assert.equal(structure.cells.length, 3, `${id} statistics`);
					assert.ok(structure.cells.every((cell) => cell.semanticPair && cell.label && cell.value !== undefined && cell.valueAboveLabel), `${id}: statistic values and labels are not clearly paired`);
					if (id === "delivery-queue") {
						assert.equal(structure.mainCount, 0, "delivery queue must not render a redundant main sum");
						const stats = await request(page, "/settings/notification-queue/stats");
						assert.equal(stats.status, 200, "delivery queue stats endpoint failed");
						const failed = Number.isInteger(stats.data.failed_recent) && stats.data.failed_recent >= 0 ? stats.data.failed_recent : stats.data.failed;
						assert.deepEqual(structure.cells.map((cell) => cell.label), ["Pending", "Processing", "Failed (24 h)"]);
						assert.deepEqual(structure.cells.map((cell) => cell.value), [String(stats.data.pending), String(stats.data.processing), String(failed)],
							"delivery queue metric values do not match the stats API");
					} else {
						assert.equal(structure.mainCount, 0, "alerts must not render a main number that repeats the header count");
						assert.equal(structure.title, "Alerts");
						assert.ok(structure.status.endsWith("3 open"), "alerts header does not show the open count");
						assert.deepEqual(structure.cells.map((cell) => [cell.label, cell.value]),
							[["Acknowledged (24 h)", "2"], ["Started (24 h)", "5"], ["Resolved (24 h)", "4"]], "alert metrics do not match the summary");
					}
				}
				const shotsDir = process.env.JUSTUP_LAYOUT_SHOTS;
				if (shotsDir) {
					for (const id of ['downtime', 'alerts-open', 'delivery-queue']) {
						await page.locator(`[data-widget-id="${id}"] .grid-stack-item-content`).screenshot({
							path: path.join(shotsDir, `${browserName}-widget-${id}.png`),
						});
					}
				}
			}
			assert.deepEqual(errors, [], "dashboard raised browser errors");
			console.log(`PASS ${browserName}/${role}: GridStack edit, keyboard move/resize, drag, hide, save, reload, responsive reflow, cancel and reset`);
		} catch (error) {
			const animations = await page.evaluate(() => document.getAnimations().filter((animation) => animation.playState === "running")
				.map((animation) => ({ name: animation.animationName || animation.transitionProperty,
					target: animation.effect?.target?.className, time: animation.currentTime })));
			error.message += `; running animations: ${JSON.stringify(animations)}`;
			throw error;
		} finally {
			const shotsDir = process.env.JUSTUP_LAYOUT_SHOTS;
			if (shotsDir) {
				mkdirSync(shotsDir, { recursive: true });
				await page.screenshot({ path: path.join(shotsDir, `${browserName}-gridstack-${role}.png`), fullPage: true }).catch(() => {});
			}
			await context.close();
		}
	}
	// A stored V2 layout written before a minimum height was raised (Monitor Quality: 2 -> 3 rows). The
	// server rejects such a size on save, so the client has to raise it while loading, keep the other
	// positions, resolve the overlap and never provoke a 422 when it saves again.
	if (phase === "legacy") for (const hidden of [false, true]) {
		const legacyUser = `legacy-${hidden ? "hidden" : "shown"}-${browserName}`;
		const setupContext = await browser.newContext({ storageState: adminState });
		try {
			const setupPage = await setupContext.newPage();
			await setupPage.goto(app.base + "/");
			assert.equal((await request(setupPage, "/users", "POST", { username: legacyUser, password, is_admin: false })).status, 200);
		} finally {
			await setupContext.close();
		}
		const rows = [
			["status-online", 0, 0, 1, 2, 1], ["status-offline", 3, 0, 3, 2, 1], ["status-disabled", 6, 0, 3, 2, 1], ["status-total", 9, 0, 3, 2, 1],
			["quality", 0, 2, 3, 2, hidden ? 0 : 1], ["downtime", 3, 2, 6, 4, 1], ["alerts-open", 0, 4, 3, 3, 1],
		];
		const stored = spawnSync(app.python, ["-c", `import json, sqlite3, sys
db = sqlite3.connect(sys.argv[1] + "/sqlite/justup.db")
uid = db.execute("SELECT id FROM users WHERE username = ?", (sys.argv[2],)).fetchone()[0]
for pos, (wid, x, y, w, h, visible) in enumerate(json.loads(sys.argv[3])):
	db.execute("INSERT INTO dashboard_layouts (user_id, widget_id, position, x, y, w, h, visible, layout_version, height_mode) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 2, 'auto')", (uid, wid, pos, x, y, w, h, visible))
db.commit()
`, app.data, legacyUser, JSON.stringify(rows)], { cwd: repo, encoding: "utf8" });
		assert.equal(stored.status, 0, `could not store the legacy layout: ${stored.stderr}`);
		const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
		const page = await context.newPage();
		const label = `${browserName}/legacy ${hidden ? "hidden" : "visible"} quality`;
		try {
			await page.goto(app.base + "/ui/login");
			await page.getByLabel("Username").fill(legacyUser);
			await page.getByLabel("Password", { exact: true }).fill(password);
			await page.locator('#login-form button[type="submit"]').click();
			await page.waitForURL((url) => !/login|change-password/.test(url.pathname), { timeout: 10000 });
			await page.goto(app.base + "/");
			await ready(page);
			assert.equal((await node(page.locator('[data-widget-id="status-online"]'))).w, 2, `${label}: narrow saved hero was not raised to its minimum width`);
			const quality = page.locator('[data-widget-id="quality"]');
			if (!hidden) {
				assert.ok((await node(quality)).h >= 3, `${label}: height was not raised to the minimum`);
				assert.equal(await page.locator('[data-widget-id="downtime"]').evaluate((element) => element.gridstackNode.x), 3, `${label}: neighbour position was not kept`);
				await expectGridGeometry(page, `${label} after loading`);
			}
			await page.getByRole("button", { name: "Edit dashboard", exact: true }).click();
			const downtime = page.locator('[data-widget-id="downtime"]');
			const downtimeBefore = await node(downtime);
			await downtime.getByRole("button", { name: /^Move or resize widget: Downtime overview\./ }).press("ArrowDown");
			assert.ok((await node(downtime)).y > downtimeBefore.y, `${label}: edit did not move the widget`);
			const savedResponse = page.waitForResponse((response) => response.url().endsWith("/me/dashboard-layout") && response.request().method() === "PUT");
			await page.getByRole("button", { name: "Done", exact: true }).click();
			const saveResult = await savedResponse;
			assert.equal(saveResult.status(), 200, `${label}: saving was rejected: ${await saveResult.text()}`);
			await page.reload();
			await ready(page);
			const layout = (await request(page, "/me/dashboard-layout")).data.layout;
			const savedQuality = layout.widgets.find((widget) => widget.id === "quality");
			assert.ok(savedQuality.h >= 3, `${label}: stored height is still below the minimum`);
			assert.equal(savedQuality.visible, !hidden);
			assert.equal(layout.widgets.find((widget) => widget.id === "status-online").w, 2, `${label}: raised hero width was not saved`);
			const visibleBoxes = layout.widgets.filter((widget) => widget.visible);
			for (const a of visibleBoxes) for (const b of visibleBoxes) {
				if (a.id < b.id) assert.ok(a.x + a.w <= b.x || b.x + b.w <= a.x || a.y + a.h <= b.y || b.y + b.h <= a.y, `${label}: ${a.id} overlaps ${b.id}`);
			}
			assert.equal(layout.widgets.find((widget) => widget.id === "status-total").x, 9, `${label}: unrelated position changed`);
			await geometry(page);
			await expectGridGeometry(page, `${label} after reload`);
		} finally {
			await context.close();
		}
	}
	// Every widget at its registry minimum (manual height) and at its default size must fit its content
	// without a scrollbar, in both themes and before and after edit mode.
	if (phase === "sizes") {
		const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, storageState: adminState });
		const page = await context.newPage();
		try {
			await page.goto(app.base + "/");
			await ready(page);
			for (const sizing of ["minimum", "default"]) {
				const layout = await page.evaluate((mode) => {
					let y = 0;
					return {
						version: 2,
						widgets: window.JuDashboardRegistry.forRole(true).map((widget) => {
							const size = mode === "minimum" ? { w: widget.minW, h: widget.minH } : widget.defaultSize;
							const placed = { id: widget.id, x: 0, y, w: size.w, h: size.h, visible: true, height_mode: mode === "minimum" ? "manual" : "auto" };
							y += size.h;
							return placed;
						}),
					};
				}, sizing);
				assert.equal((await request(page, "/me/dashboard-layout", "PUT", layout)).status, 200);
				await page.reload();
				await ready(page);
				if (sizing === "minimum") {
					// The Queue tile at its registry minimum width (3 columns, minW 3) keeps its three-row minimum without
					// clipping or scrolling; the stacked-metrics container query must not add rows. 1280x720 at 200 % zoom is 640x360.
					for (const viewport of [{ width: 992, height: 768 }, { width: 1024, height: 768 }, { width: 640, height: 360 }]) {
						await page.setViewportSize(viewport);
						await ready(page);
						for (const theme of ["light", "dark"]) {
							await page.evaluate((value) => document.documentElement.setAttribute("data-bs-theme", value), theme);
							await waitForVisualStability(page);
							const label = `${browserName} Queue at minimum width ${viewport.width}x${viewport.height} ${theme}`;
							const measured = await expectQueueContent(page, label);
							if (viewport.width >= 992) assert.equal(measured.cols, 4, `${label}: Queue is not at its 4-column minimum ${JSON.stringify(measured)}`);
							console.log(`MEASURED ${label}: ${JSON.stringify(measured)}`);
						}
					}
					await page.setViewportSize({ width: 1440, height: 1000 });
					await ready(page);
				}
				for (const editing of [false, true]) {
					if (editing) await page.getByRole("button", { name: "Edit dashboard", exact: true }).click();
					for (const theme of ["light", "dark"]) {
						await page.evaluate((value) => document.documentElement.setAttribute("data-bs-theme", value), theme);
						await waitForVisualStability(page);
						// A manual height below the content may scroll inside the declared body area, never a KPI tile.
						const allowed = (issue) => sizing === "minimum" && issue.node.includes("dashboard-widget-body") && !issue.node.includes("hero");
						const scrolling = (await runAudit(page, "top")).issues.filter((issue) => issue.type === "widget-scrollbar" && !allowed(issue));
						assert.deepEqual(scrolling, [], `${browserName} ${sizing} sizes, ${theme}, ${editing ? "edit" : "view"} mode: widget scrollbar`);
					}
				}
				await page.keyboard.press("Escape");
			}
			assert.equal((await request(page, "/me/dashboard-layout", "DELETE")).status, 200);
		} finally {
			await context.close();
		}
	}
	if (phase === "roles") {
		const verify = await browser.newContext({ storageState: adminState });
		try {
			const page = await verify.newPage();
			await page.goto(app.base + "/");
			assert.deepEqual((await request(page, "/me/dashboard-layout")).data.layout, adminLayout, "viewer changes modified the administrator's layout");
		} finally {
			await verify.close();
		}
	}
}

// The content wrapper clips x overflow as a documented last resort; lift it so the audit sees the
// real overflow and root causes cannot hide behind it.
// Playwright's trace snapshotter (`trace: retain-on-failure` records DOM snapshots for every test)
// injects a script into each frame. The sandboxed SMTP editor and preview iframes have no
// `allow-scripts` on purpose, so Chromium and WebKit log this notice for the tool, not for the
// app. Only pages that declare `sandboxedFrames` may drop exactly this text; every other console
// error stays a finding.
const SANDBOXED_FRAME_SCRIPT_NOTICE =
	/^Blocked script execution in 'about:(?:blank|srcdoc)' because the document's frame is sandboxed and the 'allow-scripts' permission is not set\.$/;
const unexpectedConsoleErrors = (pg, messages) => (pg.sandboxedFrames
	? messages.filter((text) => !SANDBOXED_FRAME_SCRIPT_NOTICE.test(text)) : messages);
const UNGUARD = ".ju-content { overflow-x: visible !important; }";
const auditSrc = readFileSync(path.join(here, "audit.js"), "utf8").replace(/^\/\/.*\n/gm, "");
const runAudit = (page, scrollPos) => page.evaluate(`(${auditSrc})(${JSON.stringify({ ...CFG, scrollPos })})`);

// Shell contract self-test: one long editor page (two `.ui-form-actions` bars with Cancel and Save) is
// audited in the shared viewport shell, which must stay free of shell findings, and in shells with
// one injected defect each, which must report the matching finding class.
async function auditShellSelfTest(context) {
	const shellTypes = ["shell-viewport-overflow", "document-scrolls", "topbar-outside-viewport", "footer-outside-viewport", "footer-above-viewport-bottom", "main-not-scrollable", "form-actions-unreachable", "form-actions-covered"];
	const css = readFileSync(path.join(repo, "app/static/css/gentelella-layout.css"), "utf8");
	const body = `<div id="app-shell" class="ju-shell"><div class="ju-content">
		<header class="ju-topbar">Top</header>
		<main id="main-content"><form><div style="height:3000px">Long editor</div>
			<div class="ui-form-actions" style="position:sticky;bottom:0"><a href="#">Cancel</a> <button type="submit">Save</button></div>
			<div class="ui-form-actions"><a href="#">Back</a> <button type="button">Apply</button></div></form></main>
		<footer class="ju-footer">Footer</footer></div></div>`;
	const run = async (extraCss, scrollPos) => {
		const page = await context.newPage();
		try {
			await page.setContent(`<!doctype html><style>body{margin:0}${css}${extraCss}</style>${body}`);
			await page.evaluate((pos) => {
				const main = document.getElementById("main-content");
				main.scrollTo({ top: pos === "bottom" ? main.scrollHeight : 0, behavior: "instant" });
				window.scrollTo({ top: pos === "bottom" ? document.documentElement.scrollHeight : 0, behavior: "instant" });
			}, scrollPos);
			return new Set((await runAudit(page, scrollPos)).issues.map((finding) => finding.type));
		} finally {
			await page.close();
		}
	};
	// Correct shell, also with a safe-area style gap outside the footer box: no shell findings anywhere.
	for (const [label, extra] of [["viewport shell", ""], ["viewport shell with bottom inset", ".ju-shell{box-sizing:border-box;padding-bottom:20px}"]]) {
		for (const pos of ["top", "bottom"]) {
			const found = await run(extra, pos);
			for (const type of shellTypes) assert.ok(!found.has(type), `shell self-test: ${label} reported ${type} at ${pos}`);
		}
	}
	// One injected defect per class; each must be reported as its own finding class.
	const defects = [
		["shell grows beyond the viewport", ".ju-shell{height:calc(100vh + 120px)}", "top", ["shell-viewport-overflow", "document-scrolls"]],
		["document-flow shell", ".ju-shell{height:auto;min-height:100vh}.ju-content>main{overflow:visible}", "top", ["shell-viewport-overflow", "document-scrolls", "footer-outside-viewport"]],
		["footer visible but 80px too high", ".ju-shell{height:calc(100vh - 80px)}", "top", ["footer-above-viewport-bottom"]],
		["overflowing main is not scrollable", ".ju-content>main{overflow:hidden}", "top", ["main-not-scrollable"]],
		["footer covers the action bar", ".ju-footer{position:relative;top:-60px}", "bottom", ["form-actions-unreachable"]],
		["persistent action bar leaves main", ".ui-form-actions{bottom:-50px !important}", "top", ["form-actions-unreachable"]],
	];
	for (const [label, extra, pos, expected] of defects) {
		const found = await run(extra, pos);
		for (const type of expected) assert.ok(found.has(type), `shell self-test: "${label}" did not report ${type} (got ${[...found].join(", ") || "nothing"})`);
	}
	// The too-high footer is a different class than a footer outside the viewport.
	const high = await run(".ju-shell{height:calc(100vh - 80px)}", "top");
	assert.ok(!high.has("footer-outside-viewport") && !high.has("shell-viewport-overflow"), "shell self-test: too-high footer was misclassified");
}

// The sandbox notice is dropped for pages with sandboxed frames only, and only when the text matches exactly.
function auditConsoleFilterSelfTest() {
	const notice = "Blocked script execution in 'about:srcdoc' because the document's frame is sandboxed and the 'allow-scripts' permission is not set.";
	const other = ["Failed to load resource: the server responded with a status of 500", "Uncaught TypeError: x is not a function",
		"Blocked script execution in 'https://example.com/' because the document's frame is sandboxed and the 'allow-scripts' permission is not set.",
		`${notice} Extra`, `Prefix ${notice}`];
	const smtp = PAGES.find((entry) => entry.id === "settings-smtp-preview");
	assert.deepEqual(unexpectedConsoleErrors(smtp, [notice, notice.replace("srcdoc", "blank")]), [], "console self-test: sandbox notice not filtered on a sandboxed-frame page");
	assert.deepEqual(unexpectedConsoleErrors(smtp, other), other, "console self-test: other console errors must stay findings on a sandboxed-frame page");
	assert.deepEqual(unexpectedConsoleErrors(PAGES.find((entry) => entry.id === "dashboard"), [notice]), [notice], "console self-test: sandbox notice must stay a finding on other pages");
}
async function auditSelfTest(browser) {
	const context = await browser.newContext({ viewport: { width: 1366, height: 1024 } });
	try {
		const page = await context.newPage();
		await page.setContent(`<style>
			.target-kpi-pair { position: relative; height: 40px; }
			.target-kpi-label { position: absolute; inset: 0 auto auto 0; }
			#combined-card { position: relative; height: 60px; }
			.combined-footer { position: relative; top: 80px; height: 20px; }
			.target-combined-plot { height: 12px; }
			#availability-card { position: relative; height: 80px; }
			#availability-card > .card-body { height: 40px; padding: 0; }
			.target-availability-plot { position: relative; top: 90px; height: 10px; }
			#downtime-history-card > .card-body { height: 40px; overflow-y: auto; }
			.target-empty-state { height: 80px; }
			.ju-notif-swipe-item { position: relative; width: 300px; height: 40px; }
			.ju-notif-swipe-content { position: relative; width: 100%; height: 40px; }
			.ju-notif-dismiss-action { position: absolute; top: 0; right: 0; width: 80px; height: 40px; }
		</style><main id="main-content" class="ju-content">
			<div class="target-kpi-pair"><div><p class="target-kpi-label">Uptime 24h</p><p class="target-kpi-value">100%</p></div><div><p class="target-kpi-label">Uptime 30d</p><p class="target-kpi-value">100%</p></div></div>
			<section id="target-dashboard-pane" style="position:relative;width:400px;height:40px"><div id="target-info-cards" style="display:contents"><div class="target-kpi-row" style="width:200px;height:20px"></div></div></section>
			<div class="card" id="combined-card"><div class="target-combined-plot"></div><div class="combined-footer"></div></div>
			<div class="card" id="availability-card"><div class="card-body"><div class="target-availability-plot"><canvas id="chart-uptime"></canvas></div></div></div>
			<div id="downtime-history-card"><div class="card-body"><div class="target-empty-state"></div></div></div>
			<div data-widget-id="delivery-queue"><div class="dashboard-monitor-body" style="height:10px;overflow-y:auto"><div style="height:40px"></div></div></div>
			<div id="ghost-bar" style="width:100px;height:40px;overflow-x:auto"><div style="width:80px;height:44px"></div></div>
			<div class="ju-notif-swipe-item is-dismiss-revealed"><div class="ju-notif-swipe-content"></div><div class="ju-notif-dismiss-action"></div></div>
		</main>`);
		await page.evaluate(() => {
			window.Chart = { getChart: (canvas) => canvas.id === "chart-uptime" ? {
				width: 100, height: 100, chartArea: { left: 10, right: 90, top: 10, bottom: 90 },
				legend: { left: -2, right: 105, top: 80, bottom: 105, options: { display: true, position: "bottom" } },
			} : null };
		});
		const result = await runAudit(page, "top");
		const types = new Set(result.issues.map((finding) => finding.type));
		for (const type of ["text-overlap", "dashboard-content-outside-card", "chart-plot-too-small", "chart-plot-outside-card", "chart-legend-outside-canvas", "chart-legend-overlaps-plot", "chart-area-too-small", "chart-legend-position", "dashboard-kpi-row-does-not-fill-grid", "downtime-empty-ghost-scroll", "notification-delete-overlaps-content", "delivery-queue-ghost-scroll", "ghost-scroll-y"]) {
			assert.ok(types.has(type), `layout audit self-test did not detect ${type}`);
		}
		await auditShellSelfTest(context);
		await auditBadgeSelfTest(page);
		auditConsoleFilterSelfTest();
	} finally {
		await context.close();
	}
}

// Badge groups: skewed rows, off-centre labels, overlaps and clipping must be found, a centred group must pass.
async function auditBadgeSelfTest(page) {
	await page.setContent(`<style>
		.ju-badges { display: flex; flex-wrap: wrap; align-items: center; align-content: center; gap: 4px; width: 200px; height: 80px; }
		.badge { display: inline-flex; align-items: center; padding: 4px 8px; font: 12px/1.1 sans-serif; }
		#bad-rows { align-content: flex-start; }
		#bad-label .badge { align-items: flex-start; height: 28px; }
		#bad-overlap .badge + .badge { margin-inline-start: -20px; }
		#bad-clip { width: 40px; overflow: hidden; flex-wrap: nowrap; }
		#bad-clip .badge { flex: none; }
	</style><main id="main-content" class="ju-content">
		<div id="good" class="ju-badges"><span class="badge">Ping</span><span class="badge">HTTP</span></div>
		<div id="bad-rows" class="ju-badges"><span class="badge">Ping</span></div>
		<div id="bad-label" class="ju-badges"><span class="badge">Ping</span></div>
		<div id="bad-overlap" class="ju-badges"><span class="badge">Ping</span><span class="badge">HTTP</span></div>
		<div id="bad-clip" class="ju-badges"><span class="badge">A long badge label</span></div>
	</main>`);
	const found = new Set((await runAudit(page, "top")).issues.filter((issue) => issue.type.startsWith("badge-"))
		.map((issue) => `${issue.node}|${issue.type}`));
	for (const [node, type] of [["div#bad-rows.ju-badges", "badge-group-not-centered"], ["span.badge", "badge-text-not-centered"],
		["span.badge", "badge-overlap"], ["div#bad-clip.ju-badges", "badge-clipped-by-group"]]) {
		assert.ok(found.has(`${node}|${type}`), `badge self-test did not detect ${type} on ${node}: ${[...found]}`);
	}
	assert.equal([...found].some((entry) => entry.startsWith("div#good.")), false, "badge self-test: a centred group produced findings");
}

// Keyboard reach: focusing a control (which scrolls it into view) must not leave it under fixed UI.
const focusAudit = (page) => page.evaluate(() => {
	const bad = [];
	const sel = "main a[href], main button, main input:not([type=hidden]), main select, main textarea, main summary";
	const els = Array.from(document.querySelectorAll(sel)).filter((el) => {
		const r = el.getBoundingClientRect();
		const cs = getComputedStyle(el);
		return r.width > 2 && r.height > 2 && cs.visibility !== "hidden" && !el.disabled && !el.closest("[hidden], .visually-hidden, .modal:not(.show), fieldset:disabled, .ju-hp") && el.getAttribute("tabindex") !== "-1" && !(el.closest("details:not([open])") && !el.closest("summary"));
	});
	const step = Math.max(1, Math.floor(els.length / 60));
	for (let i = 0; i < els.length; i += step) {
		const el = els[i];
		// The root may scroll smoothly; jump so the measurement is taken at the resting position.
		el.focus({ preventScroll: true });
		el.scrollIntoView({ block: "nearest", inline: "nearest", behavior: "instant" });
		const r = el.getBoundingClientRect();
		const x = r.left + r.width / 2, y = r.top + r.height / 2;
		if (x < 0 || y < 0 || x > innerWidth || y > innerHeight) { bad.push({ type: "focus-offscreen", node: el.id || el.tagName }); continue; }
		const hit = document.elementFromPoint(x, y);
		if (hit && !(el === hit || el.contains(hit) || hit.contains(el) || (el.labels && Array.from(el.labels).some((l) => l.contains(hit))))) {
			bad.push({ type: "focus-obscured", node: el.id || el.tagName, by: hit.className || hit.tagName });
		}
	}
	const main = document.getElementById("main-content");
	if (main && document.getElementById("app-shell")) main.scrollTo({ top: 0, behavior: "instant" });
	else window.scrollTo({ top: 0, behavior: "instant" });
	return bad;
});

async function modalAudit(page) {
	return page.evaluate(async () => {
		const bad = [];
		const once = (el, evt, act) => new Promise((resolve) => { el.addEventListener(evt, resolve, { once: true }); act(); });
		for (const modal of document.querySelectorAll(".modal")) {
			const inst = window.bootstrap.Modal.getOrCreateInstance(modal);
			await once(modal, "shown.bs.modal", () => inst.show());
			// Bootstrap emits `shown` from a timer that can fire before a slow engine finished the slide-in;
			// measure the resting geometry only.
			for (let i = 0; i < 100 && modal.getAnimations({ subtree: true }).some((a) => a.playState === "running"); i++) await new Promise((r) => setTimeout(r, 30));
			await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
			const c = modal.querySelector(".modal-content").getBoundingClientRect();
			const doc = document.documentElement;
			if (c.left < -1 || c.right > doc.clientWidth + 1) bad.push({ type: "modal-exceeds-viewport-x", node: modal.id, rect: [c.left, c.right, doc.clientWidth] });
			if (doc.scrollWidth > doc.clientWidth + 2) bad.push({ type: "modal-doc-overflow-x", node: modal.id });
			const close = modal.querySelector(".btn-close, [data-bs-dismiss=modal]");
			if (close) {
				const r = close.getBoundingClientRect();
				const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
				if (!hit || !(close === hit || close.contains(hit))) bad.push({ type: "modal-close-unreachable", node: modal.id, by: hit ? `${hit.tagName}.${String(hit.className).slice(0, 60)}` : null, rect: [r.left, r.top, r.width, r.height], shown: modal.classList.contains("show") });
			}
			// Contract (DESIGN.md "Modal dialogs"): focus lifecycle, footer action, scrollable body, field geometry.
			if (!modal.contains(document.activeElement) || document.activeElement === modal) bad.push({ type: "modal-initial-focus-missing", node: modal.id });
			if (modal.querySelector("[autofocus]")) bad.push({ type: "modal-autofocus-attribute", node: modal.id });
			if (!modal.querySelector(".modal-footer [data-bs-dismiss=modal]")) bad.push({ type: "modal-footer-cancel-missing", node: modal.id });
			const foot = modal.querySelector(".modal-footer");
			if (foot && foot.getBoundingClientRect().bottom > innerHeight + 1) bad.push({ type: "modal-footer-unreachable", node: modal.id });
			const mbody = modal.querySelector(".modal-body");
			if (mbody && mbody.scrollHeight > mbody.clientHeight + 1 && getComputedStyle(mbody).overflowY === "visible" && !modal.querySelector(".modal-dialog-scrollable") && modal.querySelector(".modal-dialog").getBoundingClientRect().height >= innerHeight - 1) bad.push({ type: "modal-body-not-scrollable", node: modal.id });
			for (const el of modal.querySelectorAll("input:not([type=hidden]):not([type=checkbox]):not(.visually-hidden), select, textarea")) {
				if (!el.offsetParent) continue;
				const r = el.getBoundingClientRect();
				if (r.right > c.right + 1 || r.left < c.left - 1) bad.push({ type: "modal-field-clipped", node: `${modal.id}#${el.id}` });
				if (el.getAttribute("aria-invalid") === "true" && !(el.getAttribute("aria-describedby") || "").split(" ").some((d) => document.getElementById(d))) bad.push({ type: "modal-invalid-field-undescribed", node: `${modal.id}#${el.id}` });
			}
			await once(modal, "hidden.bs.modal", () => inst.hide());
		}
		return bad;
	});
}

async function installChartInteractionHooks(page) {
	await page.addInitScript(() => {
		window.__justUpMetricRequests = [];
		window.__justUpPendingMetricRequests = 0;
		const fetch = window.fetch.bind(window);
		window.fetch = (input, init) => {
			const rawUrl = typeof input === "string" ? input : input.url;
			const url = new URL(rawUrl, window.location.href);
			const isMetric = /^\/targets\/\d+\/metrics\//.test(url.pathname) && url.searchParams.get("display") === "true";
			if (!isMetric) return fetch(input, init);
			window.__justUpMetricRequests.push(url.href);
			window.__justUpPendingMetricRequests += 1;
			return fetch(input, init).finally(() => { window.__justUpPendingMetricRequests -= 1; });
		};
		class TestEventSource {
			constructor(url) {
				this.url = url;
				this.listeners = {};
				window.__justUpEventSource = this;
			}
			addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
			close() {}
			emit(type, data = {}) {
				for (const listener of this.listeners[type] || []) listener({ type, data: JSON.stringify(data) });
			}
		}
		window.EventSource = TestEventSource;
	});
}

// Opens the custom date range with the keyboard: its fields must stay inside the viewport and
// opening them must not move the controls or the charts.
async function auditTargetToolbarInteractions(page) {
	const summary = page.locator("#target-custom-range > summary");
	await summary.scrollIntoViewIfNeeded();
	const layout = () => page.evaluate(() => {
		const top = (selector) => Math.round(document.querySelector(selector).getBoundingClientRect().top * 10) / 10;
		return [".target-toolbar", "#target-custom-range > summary", "#reset-zoom-btn", ".target-combined-card"].map(top);
	});
	const closed = await layout();
	await summary.focus();
	await page.keyboard.press("Enter");
	assert.ok(await page.locator("#target-custom-range").evaluate((el) => el.open), "custom range did not open with Enter");
	await waitForVisualStability(page);
	const geometry = await page.evaluate(() => {
		const rect = (selector) => document.querySelector(selector).getBoundingClientRect();
		const fields = [rect("#range-from"), rect("#range-to"), rect("#load-range-btn")];
		return {
			inside: fields.every((r) => r.width > 0 && r.left >= 0 && r.right <= window.innerWidth),
			overflowX: document.documentElement.scrollWidth > document.documentElement.clientWidth,
		};
	});
	assert.ok(geometry.inside, "custom range fields leave the viewport horizontally");
	assert.ok(!geometry.overflowX, "opening the custom range causes horizontal page overflow");
	assert.deepEqual(await layout(), closed, "opening the custom range moved the toolbar controls or the charts");
	await summary.focus();
	await page.keyboard.press("Enter");
	assert.ok(!(await page.locator("#target-custom-range").evaluate((el) => el.open)), "custom range did not close with Enter");
	await waitForVisualStability(page);
}

async function auditCombinedChartInteractions(page) {
	await page.locator("#chart-combined").scrollIntoViewIfNeeded();
	await waitForVisualStability(page);
	const snapshot = () => page.evaluate(() => {
		const canvas = document.getElementById("chart-combined");
		const chart = canvas && window.Chart?.getChart(canvas);
		if (!chart?.chartArea) return null;
		const rect = canvas.getBoundingClientRect();
		return {
			min: chart.scales.x.min,
			max: chart.scales.x.max,
			series: chart.data.datasets.find((dataset) => dataset._kind === "ms")?.data.map((point) => [point.x, point.y]),
			area: {
				left: rect.left + chart.chartArea.left,
				right: rect.left + chart.chartArea.right,
				top: rect.top + chart.chartArea.top,
				bottom: rect.top + chart.chartArea.bottom,
			},
		};
	});
	const original = await snapshot();
	assert.ok(original, "combined chart did not initialize");
	const requestCount = () => page.evaluate(() => window.__justUpMetricRequests.length);
	const pendingRequests = () => page.evaluate(() => window.__justUpPendingMetricRequests);
	const waitForRangeRequests = async (before) => {
		await page.waitForFunction((count) =>
			window.__justUpMetricRequests.length >= count + 4 && window.__justUpPendingMetricRequests === 0,
		before, { timeout: 15000 });
	};
	const dragZoom = async (before, leftFraction, rightFraction) => {
		const count = await requestCount();
		const y = (before.area.top + before.area.bottom) / 2;
		await page.mouse.move(before.area.left + (before.area.right - before.area.left) * leftFraction, y);
		await page.mouse.down();
		await page.mouse.move(before.area.left + (before.area.right - before.area.left) * rightFraction, y, { steps: 10 });
		await page.mouse.up();
		await page.waitForFunction((range) => {
			const chart = window.Chart.getChart(document.getElementById("chart-combined"));
			return chart.scales.x.min > range.min && chart.scales.x.max < range.max;
		}, { min: before.min, max: before.max }, { timeout: 10000 });
		await waitForRangeRequests(count);
	};

	await dragZoom(original, 0.18, 0.78);
	const firstZoom = await snapshot();
	assert.ok(firstZoom.min > original.min && firstZoom.max < original.max, "first drag did not zoom the chart");
	await dragZoom(firstZoom, 0.2, 0.75);
	const secondZoom = await snapshot();
	assert.ok(secondZoom.min > firstZoom.min && secondZoom.max < firstZoom.max, "second drag did not zoom further");
	await page.waitForFunction(() => /zoomed range/i.test(document.getElementById("combined-summary")?.textContent || ""), null, { timeout: 5000 });

	const countBeforeResync = await requestCount();
	await page.evaluate(() => {
		window.__justUpChartBeforeResync = window.Chart.getChart(document.getElementById("chart-combined"));
	});
	await page.evaluate(() => window.__justUpEventSource.emit("system.resync"));
	await page.evaluate(() => new Promise((resolve) => setTimeout(resolve, 0)));
	assert.equal(await requestCount(), countBeforeResync, "live resync reloaded chart metrics while zoomed");
	assert.equal(await pendingRequests(), 0);
	const afterResync = await snapshot();
	assert.equal(await page.evaluate(() => window.Chart.getChart(document.getElementById("chart-combined")) === window.__justUpChartBeforeResync), true);
	assert.equal(afterResync.min, secondZoom.min);
	assert.equal(afterResync.max, secondZoom.max);

	const resetX = (secondZoom.area.left + secondZoom.area.right) / 2;
	const resetY = (secondZoom.area.top + secondZoom.area.bottom) / 2;
	await page.evaluate(() => {
		const chart = window.Chart.getChart(document.getElementById("chart-combined"));
		const reloadVisible = chart._reloadVisible;
		window.__justUpResetReloads = 0;
		chart._reloadVisible = function () {
			window.__justUpResetReloads += 1;
			return reloadVisible.apply(this, arguments);
		};
	});
	await page.mouse.dblclick(resetX, resetY);
	await page.waitForFunction((range) => {
		const chart = window.Chart.getChart(document.getElementById("chart-combined"));
		return chart.scales.x.min === range.min && chart.scales.x.max === range.max;
	}, { min: original.min, max: original.max }, { timeout: 10000 });
	assert.ok(await page.evaluate(() => window.__justUpResetReloads > 0), "double-click did not reload the original chart data");
	assert.deepEqual((await snapshot()).series, original.series, "double-click reset did not restore the original chart data");
	console.log("PASS Chromium: repeated chart zoom, live resync preservation and double-click reset");
}

async function auditPage(browser, app, state, browserName, vp, pg) {
	if (vp.missing) throw new Error(`Missing viewport descriptor: ${vp.name}`);
	const descriptor = { ...(vp.descriptor || {}) };
	if (browserName === "firefox" && descriptor.isMobile) delete descriptor.isMobile;
	const context = await browser.newContext({
		...descriptor,
		viewport: { width: vp.w, height: vp.h }, deviceScaleFactor: vp.dsf,
		storageState: pg.anon ? undefined : state,
	});
	const findings = [];
	const errors = [];
	const consoleErrors = [];
	try {
		const page = await context.newPage();
		const chartInteractionCheck = pg.id === "target-dashboard-kpi" && vp.name === VIEWPORTS[0].name;
		page.on("pageerror", (error) => errors.push(String(error)));
		page.on("console", (message) => { if (message.type() === "error") consoleErrors.push(message.text()); });
		try {
			if (chartInteractionCheck) await installChartInteractionHooks(page);
			await page.goto(app.base + pg.path, { waitUntil: "load" });
			await page.addStyleTag({ content: UNGUARD });
			if (pg.prepare) await pg.prepare(page);
			if (pg.id.startsWith("target-dashboard")) await waitForTargetDashboard(page);
			if (process.env.JUSTUP_LAYOUT_INSPECT === "1" && pg.id.startsWith("target-dashboard")) {
				console.log(`INSPECT ${browserName} ${vp.name} ${pg.id}: ${JSON.stringify(await inspectTargetDashboard(page))}`);
			}
			if (chartInteractionCheck) await auditCombinedChartInteractions(page);
			if (pg.id === "target-dashboard-data") await auditTargetToolbarInteractions(page);
			await waitForVisualStability(page);
			if (vp.name === VIEWPORTS[0].name) {
				const axe = await withDeadline(new AxeBuilder({ page }).exclude("iframe[sandbox]").analyze(), 60000, "axe analyze");
				for (const violation of axe.violations) findings.push({
					browser: browserName, vp: vp.name, page: pg.id, variant: "axe", type: "axe-violation",
					node: violation.id, impact: violation.impact, help: violation.help,
					targets: violation.nodes.slice(0, 5).map((node) => node.target.join(" ")),
				});
			}
			const desktop = vp.w >= 992;
			const variants = [["open", "light"], ["open", "dark"], ...(desktop ? [["rail", "light"], ["rail", "dark"]] : [])];
			for (const [nav, theme] of variants) {
				await page.evaluate(([mode, color]) => {
					document.documentElement.classList.toggle("ju-nav-sm", mode === "rail");
					document.documentElement.setAttribute("data-bs-theme", color);
				}, [nav, theme]);
				await waitForVisualStability(page);
				if (pg.id === "dashboard" || pg.id === "login") await assertBranding(page, pg.id, vp.w, theme);
				for (const position of ["top", "bottom"]) {
					await page.evaluate((pos) => {
						const main = document.getElementById("main-content");
						if (main && document.getElementById("app-shell")) main.scrollTo({ top: pos === "top" ? 0 : main.scrollHeight, behavior: "instant" });
						else window.scrollTo({ top: pos === "top" ? 0 : document.documentElement.scrollHeight, behavior: "instant" });
					}, position);
					await waitForVisualStability(page);
					const result = await runAudit(page, position);
					for (const issue of result.issues) findings.push({ browser: browserName, vp: vp.name, page: pg.id, variant: `${nav}/${theme}/${position}`, ...issue });
				}
				await page.evaluate(() => document.getElementById("app-shell")
					? document.getElementById("main-content").scrollTo({ top: 0, behavior: "instant" })
					: window.scrollTo({ top: 0, behavior: "instant" }));
				if (nav === "open" && theme === "light") {
					for (const issue of await focusAudit(page)) findings.push({ browser: browserName, vp: vp.name, page: pg.id, variant: "focus", ...issue });
				}
			}
			if (pg.modals) {
				for (const issue of await modalAudit(page)) findings.push({ browser: browserName, vp: vp.name, page: pg.id, variant: "modal", ...issue });
			}
			const shotsDir = process.env.JUSTUP_LAYOUT_SHOTS;
			if (shotsDir && pg.shot) {
				mkdirSync(shotsDir, { recursive: true });
				await page.evaluate(() => {
					document.documentElement.setAttribute("data-bs-theme", "light");
					document.documentElement.classList.remove("ju-nav-sm");
					if (document.getElementById("app-shell")) document.getElementById("main-content").scrollTo({ top: 0, behavior: "instant" });
					else window.scrollTo({ top: 0, behavior: "instant" });
				});
				await page.screenshot({ path: path.join(shotsDir, `${browserName}-${pg.id}-${vp.name.replace("@", "_")}.png`) });
			}
			for (const error of errors) findings.push({ browser: browserName, vp: vp.name, page: pg.id, variant: "js", type: "page-error", node: error.slice(0, 160) });
		} catch (error) {
			findings.push({ browser: browserName, vp: vp.name, page: pg.id, variant: "-", type: "page-failed", node: String(error.message).split("\n")[0] });
		}
		for (const error of unexpectedConsoleErrors(pg, consoleErrors)) findings.push({ browser: browserName, vp: vp.name, page: pg.id, variant: "console", type: "console-error", node: error.slice(0, 160) });
		return findings;
	} finally {
		await context.close();
	}
}

// Scrollbar emulation. "classic" keeps the engine's default bars (layout width > 0), "thin" applies
// `scrollbar-width: thin` to every element and "hidden" turns the bars off through CDP (no layout width:
// layout-equivalent to overlay scrollbars, which also take no layout space). Chromium on Linux ignores the
// OverlayScrollbar switches (verified: width stays 15px), so real overlay painting is not emulated. WebKit offers
// no switch: it runs once, with whatever the build paints.
const SCROLLBAR_MODES = { chromium: ["classic", "thin", "hidden"], webkit: ["native"], firefox: ["native"] };
async function applyScrollbarMode(page, mode) {
	if (mode === "hidden") await (await page.context().newCDPSession(page)).send("Emulation.setScrollbarsHidden", { hidden: true });
	if (mode === "thin") await page.addInitScript(() => {
		const style = document.createElement("style");
		style.textContent = "* { scrollbar-width: thin; }";
		document.addEventListener("DOMContentLoaded", () => document.head.append(style));
	});
}
// Layout width of a classic scrollbar in this browser; 0 for overlay or hidden bars.
const probeScrollbar = (page) => page.evaluate(() => {
	const probe = document.createElement("div");
	probe.style.cssText = "position:absolute;visibility:hidden;width:100px;height:100px;overflow:scroll";
	document.body.append(probe);
	const width = probe.offsetWidth - probe.clientWidth;
	probe.remove();
	return width;
});

export {
	auditDashboardGrid,
	auditPage,
	auditSelfTest,
	applyScrollbarMode,
	probeScrollbar,
	SCROLLBAR_MODES,
	PAGES,
	seed,
	startApp,
	VIEWPORTS,
	waitForVisualStability,
};
