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
//                             [--pages smtp,dashboard] [--shots DIR] [--json FILE]
//
// Playwright is a dev-only tool and is never installed by the repo: it is resolved from the
// working directory, then from $PLAYWRIGHT_DIR (for example /opt/ux-test-tools).
// Exit codes: 0 = clean, 1 = layout findings, 77 = skipped or incomplete coverage (Playwright
// missing, no browser could be started, or an explicitly requested --browsers entry is missing).
// A run that checked no page must never look green, so CI must treat 77 as "not verified".
// Sign-in and the seed target are created once per app instance; every browser then gets a fresh
// context from the stored session state (the bootstrap password is single-use).
import { spawn } from "node:child_process";
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
const VIEWPORTS = profile === "full"
	? [plain(1024, 768), plain(1280, 720), plain(1366, 768), plain(1440, 900), plain(1920, 1080), plain(768, 1024), plain(390, 844),
	plain(1536, 864), zoom(1920, 1080, 1.25), zoom(1920, 1080, 1.5), zoom(1920, 1080, 2), zoom(1366, 768, 1.25)]
	: [plain(1024, 768), plain(1280, 720), plain(1920, 1080), plain(390, 844), zoom(1920, 1080, 1.5)];
// Boundary widths around the 992px sidebar breakpoint and the 576px phone breakpoint.
if (profile === "full") VIEWPORTS.push(plain(992, 700), plain(1000, 700), plain(1100, 700));

// Deliberate component decisions: horizontal scrolling that is allowed to stay local.
if (args.viewports) {
	const only = args.viewports.split(",");
	VIEWPORTS.splice(0, VIEWPORTS.length, ...VIEWPORTS.filter((v) => only.includes(v.name)));
}
const CFG = {
	scrollX: [".settings-tabs", ".nav-tabs", ".ju-notif-tabs", ".table-responsive", "pre", ".leaflet-container"],
	revealed: [".ju-notif-swipe-item"],
	clipX: [".leaflet-container", ".progress"],
};

const settingsTab = (id, extra) => ({
	id: `settings-${id}`,
	path: "/ui/settings",
	modals: id === "general",
	async prepare(page) {
		await page.click(`#${id}-tab`);
		await page.waitForFunction((pane) => {
			const el = document.querySelector(pane);
			return el && el.classList.contains("show") && el.children.length > 0 && !el.querySelector(":scope > .text-center > .spinner-border");
		}, `#${id}-pane`, { timeout: 10000 });
		if (extra) await extra(page);
		await page.waitForTimeout(400);
	},
});
const PAGES = [
	{ id: "login", path: "/ui/login", anon: true },
	{ id: "dashboard", path: "/", modals: true, shot: true },
	{ id: "targets", path: "/ui/targets" },
	{ id: "target-new", path: "/ui/targets/new" },
	{ id: "target-edit", path: "/ui/targets/1/edit" },
	{ id: "target-dashboard", path: "/ui/targets/1", shot: true },
	{
		id: "target-network", path: "/ui/targets/1",
		async prepare(page) { await page.click("#target-network-diagnostics-tab"); await page.waitForTimeout(1500); },
	},
	{ id: "groups", path: "/ui/groups", modals: true },
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
	proc.stdout.on("data", (d) => { log += d; });
	proc.stderr.on("data", (d) => { log += d; });
	const base = `http://127.0.0.1:${port}`;
	for (let i = 0; i < 120; i++) {
		if (proc.exitCode !== null) throw new Error(`app exited early:\n${log.slice(-1500)}`);
		try { if ((await fetch(`${base}/ui/login`)).ok) break; } catch { /* not up yet */ }
		await sleep(500);
	}
	const credFile = path.join(data, ".bootstrap-admin-credentials");
	const bootstrap = JSON.parse(readFileSync(credFile, "utf8"));
	const stop = async () => { proc.kill("SIGTERM"); await sleep(800); if (proc.exitCode === null) proc.kill("SIGKILL"); rmSync(data, { recursive: true, force: true }); };
	return { base, bootstrap, stop };
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
	const res = await page.evaluate(async () => {
		const r = await apiFetch("/targets", {
			method: "POST", headers: { "Content-Type": "application/json" },
			body: JSON.stringify({ name: "Example Web", group: "Production", host: "example.com", enable_http_check: true, http_url: "https://example.com/" }),
		});
		return r.status;
	});
	if (res !== 201) throw new Error(`seeding a target failed: HTTP ${res}`);
	const state = await ctx.storageState();
	await ctx.close();
	return state;
}

// The content wrapper clips x overflow as a documented last resort; lift it so the audit sees the
// real overflow and root causes cannot hide behind it.
const UNGUARD = ".ju-content { overflow-x: visible !important; }";
const auditSrc = readFileSync(path.join(here, "audit.js"), "utf8").replace(/^\/\/.*\n/gm, "");
const runAudit = (page, scrollPos) => page.evaluate(`(${auditSrc})(${JSON.stringify({ ...CFG, scrollPos })})`);

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

async function main() {
	const browsers = (args.browsers || "chromium").split(",");
	const wanted = args.pages ? args.pages.split(",") : null;
	const pages = PAGES.filter((p) => !wanted || wanted.some((w) => p.id.includes(w)));
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
				browser = await pw[name].launch({ ignoreDefaultArgs: name === "chromium" ? ["--hide-scrollbars"] : [] });
			} catch (e) {
				console.log(`SKIP ${name}: ${String(e.message).split("\n")[0]}`);
				missing.push(name);
				continue;
			}
			launched.push(name);
			state ??= await seed(browser, app);
			for (const vp of VIEWPORTS) {
				for (const pg of pages) {
					const ctx = await browser.newContext({
						viewport: { width: vp.w, height: vp.h }, deviceScaleFactor: vp.dsf,
						storageState: pg.anon ? undefined : state,
					});
					const page = await ctx.newPage();
					const errors = [];
					const consoleErrors = [];
					page.on("pageerror", (e) => errors.push(String(e)));
					page.on("console", (msg) => { if (msg.type() === "error") consoleErrors.push(msg.text()); });
					try {
						await page.goto(app.base + pg.path, { waitUntil: "load" });
						await page.addStyleTag({ content: UNGUARD });
						if (pg.prepare) await pg.prepare(page);
						await page.waitForTimeout(250);
						if (vp.name === VIEWPORTS[0].name) {
							// axe cannot inject into the sandboxed srcdoc previews and then waits forever.
							const axe = await new AxeBuilder({ page }).exclude("iframe[sandbox]").analyze();
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
							await page.waitForTimeout(300);
							for (const pos of ["top", "bottom"]) {
								await page.evaluate((p) => window.scrollTo({ top: p === "top" ? 0 : document.documentElement.scrollHeight, behavior: "instant" }), pos);
								await page.waitForTimeout(80);
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
					} catch (e) {
						findings.push({ browser: name, vp: vp.name, page: pg.id, variant: "-", type: "page-failed", node: String(e.message).split("\n")[0] });
					}
					for (const e of consoleErrors) findings.push({ browser: name, vp: vp.name, page: pg.id, variant: "console", type: "console-error", node: e.slice(0, 160) });
					await ctx.close();
				}
			}
			await browser.close();
		}
	} finally {
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
	if (!launched.length || (args.browsers && missing.length)) process.exit(SKIP);
	process.exit(0);
}
main().catch((e) => { console.error(e); process.exit(2); });
