//
// tests/modal/run.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Browser regression check for the modal dismissal policy: a click, drag-release or tap outside the
// dialog must not close any modal; Escape and the explicit Cancel/Close controls must.
// It serves the real Bootstrap bundle, ui.css, app.js and settings/modals.html from a throw-away
// static server, so no application login or database is needed.
//
//   node tests/modal/run.mjs [--browsers chromium,webkit,firefox]
//
// Playwright is a dev-only tool resolved from the working directory, then $PLAYWRIGHT_DIR.
// Exit codes: 0 = clean, 1 = findings, 77 = skipped (Playwright or an engine is missing).
import { createRequire } from "node:module";
import { readFileSync, existsSync } from "node:fs";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

const SKIP = 77;
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const STATIC = path.join(ROOT, "app", "static");
const MODALS = path.join(ROOT, "app", "templates", "settings", "modals.html");

function loadPlaywright() {
	for (const base of [process.cwd(), process.env.PLAYWRIGHT_DIR].filter(Boolean)) {
		try { return createRequire(path.join(path.resolve(base), "/"))("playwright"); } catch { /* try next */ }
	}
	return null;
}

const pw = loadPlaywright();
if (!pw) { console.log("SKIP: Playwright is not installed (set PLAYWRIGHT_DIR)"); process.exit(SKIP); }

const arg = process.argv.indexOf("--browsers");
const engines = (arg > 0 ? process.argv[arg + 1] : "chromium").split(",").filter(Boolean);

const PAGE = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/static/css/bootstrap.min.css"><link rel="stylesheet" href="/static/css/ui.css">
</head><body><button type="button" id="opener">Open</button>
${readFileSync(MODALS, "utf8")}
<script src="/static/js/utils/csrf.js"></script><script src="/static/js/utils/escape.js"></script><script src="/static/js/bootstrap.bundle.min.js"></script><script src="/static/js/utils/confirm-dialog.js"></script><script src="/static/js/app.js"></script></body></html>`;

const server = http.createServer((req, res) => {
	const url = new URL(req.url, "http://localhost");
	if (url.pathname === "/") { res.setHeader("Content-Type", "text/html"); res.end(PAGE); return; }
	const file = path.normalize(path.join(ROOT, "app", url.pathname));
	if (!file.startsWith(STATIC + path.sep) || !existsSync(file)) { res.statusCode = 404; res.end(); return; }
	const types = { ".css": "text/css", ".js": "text/javascript" };
	res.setHeader("Content-Type", types[path.extname(file)] || "application/octet-stream");
	res.end(readFileSync(file));
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const base = `http://127.0.0.1:${server.address().port}/`;

const findings = [];
const missing = [];
let checked = 0;
const isShown = (page, id) => page.evaluate((i) => document.getElementById(i).classList.contains("show"), id);
const open = async (page, id) => {
	await page.focus("#opener");
	await page.evaluate((i) => new Promise((resolve) => {
		const modal = document.getElementById(i);
		modal.addEventListener("shown.bs.modal", resolve, { once: true });
		window.bootstrap.Modal.getOrCreateInstance(modal).show();
	}), id);
	await page.waitForFunction((i) => document.getElementById(i).classList.contains("show") &&
		!document.querySelector(".modal-backdrop.fade:not(.show)"), id);
};
const closed = (page, id) => page.waitForFunction(
	(i) => !document.getElementById(i).classList.contains("show") && getComputedStyle(document.getElementById(i)).display === "none",
	id, { timeout: 3000 }).then(() => true, () => false);

for (const name of engines) {
	let browser;
	try { browser = await pw[name].launch(); } catch (e) { console.log(`SKIP ${name}: ${String(e.message).split("\n")[0]}`); missing.push(name); continue; }
	const context = await browser.newContext({ viewport: { width: 390, height: 780 }, hasTouch: true });
	const page = await context.newPage();
	const errors = [];
	page.on("pageerror", (e) => errors.push(String(e)));
	await page.goto(base);
	const ids = await page.$$eval(".modal", (els) => els.map((el) => el.id));
	if (!ids.length) findings.push(`${name}: no modals found`);
	for (const id of ids) {
		const fail = (msg) => findings.push(`${name} #${id}: ${msg}`);
		checked++;
		// Outside the dialog: plain click, drag from the dialog released on the backdrop, touch tap.
		await open(page, id);
		await page.mouse.click(4, 4);
		if (!(await isShown(page, id))) fail("closed by a backdrop click");
		const box = await page.locator(`#${id} .modal-content`).boundingBox();
		await page.mouse.move(box.x + 20, box.y + 20);
		await page.mouse.down();
		await page.mouse.move(4, 4);
		await page.mouse.up();
		if (!(await isShown(page, id))) fail("closed by a drag released on the backdrop");
		await page.touchscreen.tap(4, 4);
		if (!(await isShown(page, id))) fail("closed by a touch tap on the backdrop");
		// Escape closes and returns focus to the opener.
		await page.keyboard.press("Escape");
		if (!(await closed(page, id))) fail("Escape did not close the modal");
		else if (await page.waitForFunction(() => document.activeElement?.id === "opener", null, { timeout: 1500 }).then(() => false, () => true)) fail("focus not returned to the opener");
		// Explicit controls: footer Cancel/Close and the header close button.
		for (const sel of [".modal-footer [data-bs-dismiss='modal']", ".modal-header .btn-close"]) {
			await open(page, id);
			const control = page.locator(`#${id} ${sel}`).first();
			if (!(await control.count())) { fail(`missing control ${sel}`); continue; }
			await control.click();
			if (!(await closed(page, id))) fail(`${sel} did not close the modal`);
		}
	}
	// Shared confirmation dialog (window.confirmAction): Cancel/Escape resolve false, Confirm true once, backdrop never closes.
	{
		const fail = (msg) => findings.push(`${name} confirmAction: ${msg}`);
		checked++;
		const ask = () => page.evaluate(() => {
			window.__confirmResults = [];
			window.confirmAction({ title: "T", message: "M", confirmLabel: "Do it" }).then((ok) => window.__confirmResults.push(ok));
		});
		// Focus on Cancel is set by shown.bs.modal, so it also marks the end of the show transition.
		const shown = () => page.waitForFunction(() => document.activeElement?.id === "ju-confirm-cancel" && !document.querySelector(".modal-backdrop.fade:not(.show)"));
		const result = (expected) => page.waitForFunction((e) => window.__confirmResults.length === 1 && window.__confirmResults[0] === e, expected, { timeout: 3000 }).then(() => true, () => false);
		const gone = () => closed(page, "ju-confirm-modal");
		const focusIsOpener = () => page.waitForFunction(() => document.activeElement?.id === "opener", null, { timeout: 1500 }).then(() => true, () => false);
		await page.focus("#opener");
		await ask(); await shown();
		await page.mouse.click(4, 4);
		if (!(await isShown(page, "ju-confirm-modal"))) fail("closed by a backdrop click");
		await page.keyboard.press("Escape");
		if (!(await gone()) || !(await result(false))) fail("Escape did not resolve false");
		if (!(await focusIsOpener())) fail("focus not returned to the opener after Escape");
		await ask(); await shown();
		await page.click("#ju-confirm-cancel");
		if (!(await gone()) || !(await result(false))) fail("Cancel did not resolve false");
		await ask(); await shown();
		await page.keyboard.press("Tab");
		if (!(await page.evaluate(() => document.activeElement?.id === "ju-confirm-ok"))) fail("Tab from Cancel does not reach Confirm");
		await page.keyboard.press("Enter");
		if (!(await gone()) || !(await result(true))) fail("keyboard Confirm did not resolve true");
		if (!(await focusIsOpener())) fail("focus not returned to the opener after Confirm");
		await ask(); await shown();
		await page.dblclick("#ju-confirm-ok");
		if (!(await gone()) || !(await result(true))) fail("double click on Confirm did not resolve exactly one true");
	}
	if (errors.length) findings.push(`${name}: page errors: ${errors.join(" | ")}`);
	console.log(`${name}: ${ids.length} modals checked`);
	await browser.close();
}
server.close();
if (!checked) { console.log("SKIP: no engine could be started"); process.exit(SKIP); }
if (findings.length) { console.log(findings.join("\n")); process.exit(1); }
if (missing.length) {
	console.log(`MISSING COVERAGE: ${missing.join(", ")} not started${arg > 0 ? " (explicitly requested)" : ""}`);
	process.exit(SKIP);
}
console.log(`OK: ${checked} modal checks, 0 findings`);
