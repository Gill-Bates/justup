//
// tests/layout/run.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Browser layout regression check for every application page (contract: docs/development/contributing.md,
// "Layout contract"). It starts the app on a free port with a throw-away data directory, signs in
// through the UI, then audits each page state in a viewport/zoom/sidebar/theme matrix with the
// in-page measurements of audit.js.
//
//   node tests/layout/run.mjs [--profile quick|full] [--browsers chromium,webkit,firefox]
//                             [--pages smtp,dashboard] [--page target-dashboard] [--shots DIR] [--json FILE]
//
// Playwright is a dev-only tool and is never installed by the repo: it is resolved from the
// working directory, then from $PLAYWRIGHT_DIR (for example /opt/ux-test-tools).
// Exit codes: 0 = clean, 1 = layout findings, 77 = skipped or incomplete coverage (Playwright
// missing, no browser could be started, or an explicitly requested --browsers entry is missing).
// A run that checked no page must never look green, so CI must treat 77 as "not verified".
// Sign-in and the seed target are created once per app instance; every browser then gets a fresh
// context from the stored session state (the bootstrap password is single-use).
import { spawn, spawnSync } from "node:child_process";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import net from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, "../..");
const SKIP = 77;

const args = Object.fromEntries(process.argv.slice(2).reduce((acc, a, i, all) => {
	if (a.startsWith("--")) acc.push([a.slice(2), all[i + 1] && !all[i + 1].startsWith("--") ? all[i + 1] : "1"]);
	return acc;
}, []));
const profile = args.profile || "quick";

function loadPlaywright() {
	for (const base of [process.cwd(), process.env.PLAYWRIGHT_DIR].filter(Boolean)) {
		try { return createRequire(path.join(path.resolve(base), "/"))("playwright"); } catch { /* try next */ }
	}
	return null;
}
function loadAxeBuilder() {
	for (const base of [process.cwd(), process.env.PLAYWRIGHT_DIR].filter(Boolean)) {
		try {
			const mod = createRequire(path.join(path.resolve(base), "/"))("@axe-core/playwright");
			return mod.default || mod;
		} catch { /* try next */ }
	}
	return null;
}
const pw = loadPlaywright();
if (!pw) { console.log("SKIP: Playwright is not installed (set PLAYWRIGHT_DIR)"); process.exit(SKIP); }
const AxeBuilder = loadAxeBuilder();
if (!AxeBuilder) { console.log("SKIP: @axe-core/playwright is not installed (set PLAYWRIGHT_DIR)"); process.exit(SKIP); }

// Effective CSS viewports. Browser zoom Z on a W x H window gives a (W/Z) x (H/Z) CSS viewport at
// device scale factor Z: identical layout, media queries and rem sizes as real zoom.
const zoom = (w, h, z) => ({ name: `${w}x${h}@${z * 100}%`, w: Math.round(w / z), h: Math.round(h / z), dsf: z });
const plain = (w, h) => ({ name: `${w}x${h}`, w, h, dsf: 1 });
const device = (name) => {
	const descriptor = pw.devices[name];
	if (!descriptor) return { name, missing: true };
	return { name, w: descriptor.viewport.width, h: descriptor.viewport.height, dsf: descriptor.deviceScaleFactor, descriptor };
};
const VIEWPORTS = profile === "full"
	? [plain(1024, 768), plain(1280, 720), plain(1366, 768), plain(1440, 900), plain(1920, 1080), plain(768, 1024), plain(390, 844),
	plain(1536, 864), plain(320, 640), plain(375, 667), plain(1366, 1024), plain(1024, 1366),
	device("iPad Pro 11 landscape"), device("iPad Pro 11"), zoom(1920, 1080, 1.25), zoom(1920, 1080, 1.5), zoom(1920, 1080, 2), zoom(1366, 768, 1.25)]
	: [plain(1024, 768), plain(1280, 720), plain(1366, 768), plain(1920, 1080), plain(390, 844),
	device("iPad Pro 11 landscape"), device("iPad Pro 11"), zoom(1920, 1080, 1.5)];
// Boundary widths around the 992px sidebar breakpoint and the 576px phone breakpoint.
if (profile === "full") VIEWPORTS.push(plain(992, 700), plain(1000, 700), plain(1100, 700));

// Deliberate component decisions: horizontal scrolling that is allowed to stay local.
if (args.viewports) {
	const only = args.viewports.split(",");
	const unknown = only.filter((name) => !VIEWPORTS.some((vp) => vp.name === name));
	if (unknown.length) { console.log(`MISSING VIEWPORT COVERAGE: ${unknown.join(", ")} not defined in profile ${profile}`); process.exit(SKIP); }
	VIEWPORTS.splice(0, VIEWPORTS.length, ...VIEWPORTS.filter((v) => only.includes(v.name)));
}
const CFG = {
	scrollX: [".settings-tabs", ".nav-tabs", ".ju-notif-tabs", ".table-responsive", ".ju-table-viewport", "pre", ".leaflet-container"],
	revealed: [".ju-notif-swipe-item"],
	clipX: [".leaflet-container", ".progress"],
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
			return !/Loading data/i.test(label) &&
				(Boolean(window.Chart?.getChart(canvas)) || /no data/i.test(label));
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
			downtimeFragment: rect(node("#downtime-history-body")), downtimeState: rect(node("#downtime-history-body .target-empty-state, #downtime-history-body .downtime-scroll")),
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
				return Boolean(grid?.gridstack) && !grid.classList.contains("dashboard-grid--loading") &&
					(!loading || loading.hidden || loading.classList.contains("d-none"));
			}, null, { timeout: 15000 });
		},
	},
	{ id: "targets", path: "/ui/targets", shot: true },
	{ id: "target-new", path: "/ui/targets/new" },
	{ id: "target-edit", path: "/ui/targets/1/edit" },
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
	...["general", "network", "storage", "backup", "users", "recipients", "signal", "spooler"].map((t) => settingsTab(t)),
	{ ...settingsTab("smtp", (page) => page.click("#btn-mode-preview")), id: "settings-smtp-preview", shot: true },
	{ ...settingsTab("smtp", (page) => page.click("#btn-mode-edit")), id: "settings-smtp-edit", shot: true },
	{ ...settingsTab("smtp", (page) => page.click("#btn-mode-html")), id: "settings-smtp-source", shot: true },
];

const free = () => new Promise((resolve, reject) => {
	const s = net.createServer().listen(0, "127.0.0.1", () => { const { port } = s.address(); s.close(() => resolve(port)); });
	s.on("error", reject);
});
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const withDeadline = (promise, ms, label) => {
	let timer;
	const deadline = new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(`${label} timed out after ${ms} ms`)), ms); });
	return Promise.race([promise, deadline]).finally(() => clearTimeout(timer));
};
// Browser and app server are released on SIGTERM/SIGINT too (for example `timeout 1400 node ...`), so no orphan processes or temp dirs stay behind.
const cleanups = new Set();
for (const [signal, code] of [["SIGTERM", 143], ["SIGINT", 130]]) {
	process.on(signal, () => {
		console.error(`${signal}: stopping browser and app server`);
		Promise.allSettled([...cleanups].map((fn) => fn())).finally(() => process.exit(code));
	});
}

async function startApp() {
	const port = await free();
	const data = mkdtempSync(path.join(tmpdir(), "justup-layout-"));
	const python = process.env.PYTHON || (existsSync(path.join(repo, ".venv/bin/python")) ? path.join(repo, ".venv/bin/python") : "python3");
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
	if (!res.codes.every((c) => c === 201) || !res.fixtureTargetId || !res.screenshotTargetId) throw new Error(`seeding failed: HTTP ${res.codes.join(",")}`);
	seedMeasurementFixture(app, [res.screenshotTargetId, res.fixtureTargetId]);
	const state = await ctx.storageState();
	await ctx.close();
	return state;
}

async function auditDashboardGrid(browser, app, adminState, browserName) {
	const password = "Grid-Layout-2026!";
	const username = `grid-${browserName}`;
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
				return { id: element.dataset.widgetId, left: rect.left, top: rect.top, right: rect.right,
					bottom: rect.bottom, text: content.textContent.trim(), width: rect.width, height: rect.height };
			});
			return { left: bounds.left, right: bounds.right, widgets,
				overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth };
		});
		assert.ok(snapshot.widgets.length > 0, "dashboard has no visible widgets");
		assert.ok(snapshot.overflow <= 2, `page overflows by ${snapshot.overflow}px`);
		for (const widget of snapshot.widgets) {
			assert.ok(widget.width > 0 && widget.height > 0 && widget.text, `empty widget ${widget.id}`);
			assert.ok(widget.left >= snapshot.left - 2 && widget.right <= snapshot.right + 2, `widget outside grid: ${widget.id}`);
			for (const other of snapshot.widgets) {
				if (other.id === widget.id) continue;
				const overlapX = Math.min(widget.right, other.right) - Math.max(widget.left, other.left);
				const overlapY = Math.min(widget.bottom, other.bottom) - Math.max(widget.top, other.top);
				assert.ok(overlapX <= 2 || overlapY <= 2, `widgets overlap: ${widget.id}, ${other.id}`);
			}
		}
	};
	const node = (widget) => widget.evaluate((element) => {
		const { x, y, w, h } = element.gridstackNode;
		return { x, y, w, h };
	});
	const setup = await browser.newContext({ storageState: adminState });
	try {
		const page = await setup.newPage();
		await page.goto(app.base + "/");
		assert.equal((await request(page, "/me/dashboard-layout", "DELETE")).status, 200);
		assert.equal((await request(page, "/users", "POST", { username, password, is_admin: false })).status, 200);
	} finally {
		await setup.close();
	}
	let adminLayout;
	for (const role of ["admin", "viewer"]) {
		const context = await browser.newContext({ viewport: { width: 1440, height: 1000 },
			...(role === "admin" ? { storageState: adminState } : {}) });
		const page = await context.newPage();
		const errors = [];
		page.on("pageerror", (error) => errors.push(String(error)));
		try {
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
				const trigger = page.getByRole("button", { name: /\d+ targets?/ });
				await trigger.waitFor({ state: "visible", timeout: 10000 });
				await trigger.hover();
				const tooltip = page.locator("body > .tooltip.show");
				await tooltip.waitFor({ state: "visible" });
				await trigger.focus();
				const triggerBox = await trigger.boundingBox();
				assert.ok(triggerBox, "targets trigger has no browser bounds");
				await page.mouse.move(triggerBox.x + triggerBox.width + 40, triggerBox.y + triggerBox.height / 2, { steps: 3 });
				await tooltip.waitFor({ state: "detached" });
				assert.equal(await trigger.isVisible(), true, "targets trigger disappeared while hiding its tooltip");
			}
			assert.equal(await page.locator('[data-widget-id="delivery-queue"]').count(), role === "admin" ? 1 : 0);
			await geometry(page);
			await page.getByRole("button", { name: "Edit dashboard", exact: true }).click();
			const online = page.locator('[data-widget-id="status-online"]');
			const handle = online.getByRole("button", { name: /^Move or resize widget: Online\./ });
			const original = await node(online);
			await handle.press("ArrowDown");
			assert.ok((await node(online)).y > original.y, "keyboard movement did not move the widget");
			await handle.press("Shift+ArrowRight");
			assert.ok((await node(online)).w > original.w, "keyboard resize did not resize the widget");
			await handle.scrollIntoViewIfNeeded();
			const beforeDrag = await node(online);
			const box = await handle.boundingBox();
			const cell = await page.locator("#dashboard-grid").evaluate((root) => ({
				width: root.clientWidth / root.gridstack.getColumn(), height: root.gridstack.getCellHeight(),
			}));
			await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
			await page.mouse.down();
			await page.mouse.move(box.x + box.width / 2 + cell.width, box.y + box.height / 2 + cell.height * 3, { steps: 12 });
			await page.mouse.up();
			assert.notDeepEqual(await node(online), beforeDrag, "mouse drag did not move the widget");
			await waitForVisualStability(page);
			await page.getByRole("button", { name: "Edit widgets", exact: true }).click();
			const modal = page.locator("#dashboard-widgets-modal");
			await modal.getByRole("button", { name: "Remove widget: Disabled", exact: true }).click();
			if (role === "viewer") assert.equal(await modal.getByRole("button", { name: /widget: Delivery queue/ }).count(), 0);
			await modal.getByRole("button", { name: "Close", exact: true }).last().click();
			await modal.waitFor({ state: "hidden" });
			const savedResponse = page.waitForResponse((response) => response.url().endsWith("/me/dashboard-layout") && response.request().method() === "PUT");
			await page.getByRole("button", { name: "Done", exact: true }).click();
			assert.ok((await savedResponse).ok(), "saving the dashboard failed");
			await ready(page);
			const saved = (await request(page, "/me/dashboard-layout")).data.layout;
			assert.equal(saved.widgets.find((widget) => widget.id === "status-disabled").visible, false);
			await page.reload();
			await ready(page);
			assert.deepEqual((await request(page, "/me/dashboard-layout")).data.layout, saved, "reload changed the saved layout");
			assert.equal(await page.locator('[data-widget-id="status-disabled"]').isVisible(), false);
			const storedOnline = saved.widgets.find((widget) => widget.id === "status-online");
			const restoredOnline = await node(online);
			for (const dimension of ["x", "y", "w"]) {
				assert.equal(restoredOnline[dimension], storedOnline[dimension], `reload did not restore widget ${dimension}`);
			}
			for (const viewport of [{ width: 768, height: 1024 }, { width: 390, height: 844 }, { width: 1440, height: 1000 }]) {
				await page.setViewportSize(viewport);
				if (viewport.width === 390) await page.reload();
				await ready(page);
				await geometry(page);
				assert.deepEqual((await request(page, "/me/dashboard-layout")).data.layout, saved, "responsive reflow overwrote the desktop layout");
			}
			if (role === "viewer") {
				const denied = await request(page, "/me/dashboard-layout", "PUT", { version: 1,
					widgets: [{ id: "delivery-queue", x: 0, y: 0, w: 4, h: 3, visible: true }] });
				assert.equal(denied.status, 403, "viewer can persist the protected administrator widget");
				assert.deepEqual((await request(page, "/me/dashboard-layout")).data.layout, saved);
			}
			await page.getByRole("button", { name: "Edit dashboard", exact: true }).click();
			const beforeCancel = await node(online);
			await handle.press("ArrowDown");
			await page.keyboard.press("Escape");
			assert.deepEqual((await request(page, "/me/dashboard-layout")).data.layout, saved, "cancel persisted unsaved changes");
			assert.deepEqual(await node(online), beforeCancel, "cancel did not restore the visible widget position");
			await page.getByRole("button", { name: "Edit dashboard", exact: true }).click();
			await page.getByRole("button", { name: "Reset layout", exact: true }).click();
			const resetResponse = page.waitForResponse((response) => response.url().endsWith("/me/dashboard-layout") && response.request().method() === "PUT");
			await page.getByRole("button", { name: "Done", exact: true }).click();
			assert.ok((await resetResponse).ok());
			await page.reload();
			await ready(page);
			assert.equal(await page.locator('[data-widget-id="status-disabled"]').isVisible(), true);
			await geometry(page);
			if (role === "admin") {
				adminLayout = (await request(page, "/me/dashboard-layout")).data.layout;
				await page.locator('[data-widget-id="delivery-queue"]').evaluate((tile) => {
					tile.classList.remove("d-none");
					document.dispatchEvent(new Event("dashboard:widget-shown"));
				});
				const queueBody = page.locator('[data-widget-id="delivery-queue"] .dashboard-monitor-body');
				await page.waitForFunction(() => {
					const body = document.querySelector('[data-widget-id="delivery-queue"] .dashboard-monitor-body');
					return body?.textContent.includes("Queue clear") && body.textContent.includes("Failed (24 h)");
				}, null, { timeout: 10000 });
				for (const label of ["Pending", "Processing"]) {
					assert.ok((await queueBody.innerText()).includes(label), `delivery queue is missing visible label: ${label}`);
				}
				assert.ok(!(await queueBody.innerText()).includes("Oldest pending age"));
				assert.ok(!(await queueBody.innerText()).includes("Next retry in"));
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
			if (args.shots) {
				mkdirSync(args.shots, { recursive: true });
				await page.screenshot({ path: path.join(args.shots, `${browserName}-gridstack-${role}.png`), fullPage: true }).catch(() => {});
			}
			await context.close();
		}
	}
	const verify = await browser.newContext({ storageState: adminState });
	try {
		const page = await verify.newPage();
		await page.goto(app.base + "/");
		assert.deepEqual((await request(page, "/me/dashboard-layout")).data.layout, adminLayout, "viewer changes modified the administrator's layout");
	} finally {
		await verify.close();
	}
}

// The content wrapper clips x overflow as a documented last resort; lift it so the audit sees the
// real overflow and root causes cannot hide behind it.
const UNGUARD = ".ju-content { overflow-x: visible !important; }";
const auditSrc = readFileSync(path.join(here, "audit.js"), "utf8").replace(/^\/\/.*\n/gm, "");
const runAudit = (page, scrollPos) => page.evaluate(`(${auditSrc})(${JSON.stringify({ ...CFG, scrollPos })})`);

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
		for (const type of ["text-overlap", "dashboard-content-outside-card", "chart-plot-too-small", "chart-plot-outside-card", "chart-legend-outside-canvas", "chart-legend-overlaps-plot", "chart-area-too-small", "chart-legend-position", "dashboard-kpi-row-does-not-fill-grid", "downtime-empty-ghost-scroll", "notification-delete-overlaps-content"]) {
			assert.ok(types.has(type), `layout audit self-test did not detect ${type}`);
		}
	} finally {
		await context.close();
	}
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
	window.scrollTo({ top: 0, behavior: "instant" });
	return bad;
});

async function modalAudit(page) {
	return page.evaluate(async () => {
		const bad = [];
		const once = (el, evt, act) => new Promise((resolve) => { el.addEventListener(evt, resolve, { once: true }); act(); });
		for (const modal of document.querySelectorAll(".modal")) {
			const inst = window.bootstrap.Modal.getOrCreateInstance(modal);
			await once(modal, "shown.bs.modal", () => inst.show());
			const c = modal.querySelector(".modal-content").getBoundingClientRect();
			const doc = document.documentElement;
			if (c.left < -1 || c.right > doc.clientWidth + 1) bad.push({ type: "modal-exceeds-viewport-x", node: modal.id, rect: [c.left, c.right, doc.clientWidth] });
			if (doc.scrollWidth > doc.clientWidth + 2) bad.push({ type: "modal-doc-overflow-x", node: modal.id });
			const close = modal.querySelector(".btn-close, [data-bs-dismiss=modal]");
			if (close) {
				const r = close.getBoundingClientRect();
				const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
				if (!hit || !(close === hit || close.contains(hit))) bad.push({ type: "modal-close-unreachable", node: modal.id });
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

async function main() {
	const browsers = (args.browsers || "chromium").split(",");
	if (args["self-test"]) {
		let missing = false;
		for (const name of browsers) {
			let browser;
			try {
				browser = await pw[name].launch();
			} catch (e) {
				console.log(`SKIP ${name}: ${String(e.message).split("\n")[0]}`);
				missing = true;
				continue;
			}
			try {
				await auditSelfTest(browser);
				console.log(`PASS ${name}: detected injected KPI, card, plot and ghost-scroll defects`);
			} finally {
				await browser.close();
			}
		}
		if (missing || !browsers.length) process.exit(SKIP);
		return;
	}
	const wanted = args.pages !== undefined ? args.pages.split(",").map((name) => name.trim()) : null;
	const exactPages = args.page !== undefined ? new Set(args.page.split(",").map((name) => name.trim())) : null;
	for (const name of wanted || []) {
		if (!name || !PAGES.some((entry) => entry.id.includes(name))) throw new Error(`Unknown or empty page filter: ${name}`);
	}
	for (const name of exactPages || []) {
		if (!name || !PAGES.some((entry) => entry.id === name)) throw new Error(`Unknown or empty page: ${name}`);
	}
	const pages = exactPages
		? PAGES.filter((p) => exactPages.has(p.id))
		: PAGES.filter((p) => !wanted || wanted.some((w) => p.id.includes(w)));
	if (!pages.length || !VIEWPORTS.length) throw new Error("Empty layout audit selection");
	const expected = args.gridstack ? browsers.length : browsers.length * VIEWPORTS.length * pages.length;
	let completed = 0;
	const app = await startApp();
	const findings = [];
	let audits = 0;
	let state = null;
	const launched = [];
	const missing = [];
	try {
		for (const name of browsers) {
			let browser;
			try {
				// Classic scrollbars are what desktop users get; headless hides them by default.
				browser = await pw[name].launch({
					ignoreDefaultArgs: name === "chromium" ? ["--hide-scrollbars"] : [],
					// Chromium's compositor can leave theme transitions pending in headless mode.
					args: name === "chromium" ? ["--disable-threaded-animation"] : [],
				});
			} catch (e) {
				console.log(`SKIP ${name}: ${String(e.message).split("\n")[0]}`);
				missing.push(name);
				continue;
			}
			launched.push(name);
			const closeBrowser = () => browser.close();
			cleanups.add(closeBrowser);
			await auditSelfTest(browser);
			state ??= await seed(browser, app);
			if (args.gridstack) {
				try {
					await auditDashboardGrid(browser, app, state, name);
					completed++;
					audits += 2;
				} finally {
					cleanups.delete(closeBrowser);
					await browser.close();
				}
				continue;
			}
			for (const vp of VIEWPORTS) {
				if (vp.missing) {
					missing.push(`${name}/${vp.name}: descriptor unavailable`);
					continue;
				}
				const descriptor = { ...(vp.descriptor || {}) };
				if (name === "firefox" && descriptor.isMobile) {
					delete descriptor.isMobile;
					console.log(`VIEWPORT FALLBACK ${name}/${vp.name}: tablet viewport, not mobile emulation`);
				}
				for (const pg of pages) {
					let ctx;
					try {
						ctx = await browser.newContext({
							...descriptor,
							viewport: { width: vp.w, height: vp.h }, deviceScaleFactor: vp.dsf,
							storageState: pg.anon ? undefined : state,
						});
					} catch (error) {
						missing.push(`${name}/${vp.name}/${pg.id}: ${String(error.message).split("\n")[0]}`);
						continue;
					}
					const page = await ctx.newPage();
					const chartInteractionCheck = pg.id === "target-dashboard-kpi" && vp.name === VIEWPORTS[0].name;
					const errors = [];
					const consoleErrors = [];
					page.on("pageerror", (e) => errors.push(String(e)));
					page.on("console", (msg) => { if (msg.type() === "error") consoleErrors.push(msg.text()); });
					try {
						if (chartInteractionCheck) await installChartInteractionHooks(page);
						await page.goto(app.base + pg.path, { waitUntil: "load" });
						await page.addStyleTag({ content: UNGUARD });
						if (pg.prepare) await pg.prepare(page);
						if (pg.id.startsWith("target-dashboard")) await waitForTargetDashboard(page);
						if (args.inspect && pg.id.startsWith("target-dashboard")) console.log(`INSPECT ${name} ${vp.name} ${pg.id}: ${JSON.stringify(await inspectTargetDashboard(page))}`);
						if (chartInteractionCheck) await auditCombinedChartInteractions(page);
						await waitForVisualStability(page);
						if (vp.name === VIEWPORTS[0].name) {
							// axe cannot inject into the sandboxed srcdoc previews and then waits forever.
							const axe = await withDeadline(new AxeBuilder({ page }).exclude("iframe[sandbox]").analyze(), 60000, "axe analyze");
							for (const v of axe.violations) findings.push({
								browser: name, vp: vp.name, page: pg.id, variant: "axe", type: "axe-violation",
								node: v.id, impact: v.impact, help: v.help,
								targets: v.nodes.slice(0, 5).map((n) => n.target.join(" ")),
							});
						}
						const desktop = vp.w >= 992;
						const variants = [["open", "light"], ["open", "dark"], ...(desktop ? [["rail", "light"], ["rail", "dark"]] : [])];
						for (const [nav, theme] of variants) {
							await page.evaluate(([n, t]) => {
								document.documentElement.classList.toggle("ju-nav-sm", n === "rail");
								document.documentElement.setAttribute("data-bs-theme", t);
							}, [nav, theme]);
							await waitForVisualStability(page);
							for (const pos of ["top", "bottom"]) {
								await page.evaluate((p) => window.scrollTo({ top: p === "top" ? 0 : document.documentElement.scrollHeight, behavior: "instant" }), pos);
							await waitForVisualStability(page);
								const res = await runAudit(page, pos);
								audits++;
								for (const i of res.issues) findings.push({ browser: name, vp: vp.name, page: pg.id, variant: `${nav}/${theme}/${pos}`, ...i });
							}
							await page.evaluate(() => window.scrollTo({ top: 0, behavior: "instant" }));
							if (nav === "open" && theme === "light") {
								for (const i of await focusAudit(page)) findings.push({ browser: name, vp: vp.name, page: pg.id, variant: "focus", ...i });
							}
						}
						if (pg.modals) for (const i of await modalAudit(page)) findings.push({ browser: name, vp: vp.name, page: pg.id, variant: "modal", ...i });
						if (args.shots && pg.shot) {
							mkdirSync(args.shots, { recursive: true });
							await page.evaluate(() => { document.documentElement.setAttribute("data-bs-theme", "light"); document.documentElement.classList.remove("ju-nav-sm"); window.scrollTo({ top: 0, behavior: "instant" }); });
							await page.screenshot({ path: path.join(args.shots, `${name}-${pg.id}-${vp.name.replace("@", "_")}.png`) });
						}
						for (const e of errors) findings.push({ browser: name, vp: vp.name, page: pg.id, variant: "js", type: "page-error", node: e.slice(0, 160) });
						completed++;
					} catch (e) {
						findings.push({ browser: name, vp: vp.name, page: pg.id, variant: "-", type: "page-failed", node: String(e.message).split("\n")[0] });
					}
					for (const e of consoleErrors) findings.push({ browser: name, vp: vp.name, page: pg.id, variant: "console", type: "console-error", node: e.slice(0, 160) });
					await ctx.close();
				}
			}
			cleanups.delete(closeBrowser);
			await browser.close();
		}
	} finally {
		cleanups.delete(app.stop);
		await app.stop();
	}
	if (args.json) writeFileSync(args.json, JSON.stringify(findings, null, 1));
	const byKey = new Map();
	for (const f of findings) {
		const k = `${f.type} | ${f.page} | ${f.node}${f.by ? ` <- ${f.by}` : ""}`;
		const e = byKey.get(k) || { n: 0, vps: new Set(), browsers: new Set() };
		e.n++; e.vps.add(f.vp); e.browsers.add(f.browser);
		byKey.set(k, e);
	}
	console.log(`${audits} audits, ${findings.length} findings, ${byKey.size} distinct`);
	for (const [k, e] of [...byKey].sort()) console.log(`  ${k}  [${[...e.browsers].join(",")}; ${[...e.vps].join(" ")}]`);
	if (missing.length) console.log(`MISSING COVERAGE: ${missing.join(", ")} not started${args.browsers ? " (explicitly requested)" : ""}`);
	if (findings.length) process.exit(1);
	if (!audits || !launched.length || missing.length || completed !== expected) {
		console.log(`INCOMPLETE COVERAGE: ${completed}/${expected} browser/viewport/page combinations`);
		process.exit(SKIP);
	}
	process.exit(0);
}
export { startApp, seed, waitForVisualStability };

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
	main().catch((e) => { console.error(e); process.exit(2); });
}
