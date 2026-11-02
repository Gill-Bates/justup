//
// tests/smtp-ui/run.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Browser regression check for the SMTP STARTTLS switch and the settings save helper.
// It renders the real settings/smtp.html (Jinja2 via python3) next to the real settings.core.js /
// settings.storage.js, served by a throw-away static server, and replaces window.apiFetch with a
// controllable stub, so no application login, database or mail server is needed.
//
//   node tests/smtp-ui/run.mjs [--browsers chromium,webkit,firefox]
//
// Playwright is a dev-only tool resolved from the working directory, then $PLAYWRIGHT_DIR.
// Exit codes: 0 = clean, 1 = findings, 77 = skipped (Playwright, python3/jinja2 or an engine is missing).
import { createRequire } from "node:module";
import { execFileSync } from "node:child_process";
import { readFileSync, existsSync } from "node:fs";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";
const SKIP = 77;
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const STATIC = path.join(ROOT, "app", "static");
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

// The same template the settings tab loads; `smtp` and `implicit_tls_port` mirror the backend context.
const RENDER = `
import json, sys
from jinja2 import Environment, FileSystemLoader, select_autoescape
env = Environment(loader=FileSystemLoader(sys.argv[1]), autoescape=select_autoescape(["html"]))
print(env.get_template("settings/smtp.html").render(
    smtp=json.loads(sys.argv[2]), implicit_tls_port=465, template={"subject": "s", "html": "<p>h</p>"}))
`;
const PYTHON = process.env.JUSTUP_PYTHON || "python3";
function render(smtp) {
	return execFileSync(PYTHON, ["-c", RENDER, path.join(ROOT, "app", "templates"), JSON.stringify(smtp)], { encoding: "utf8" });
}
try { render({}); } catch (e) {
	console.log(`SKIP: cannot render settings/smtp.html with ${PYTHON} (${String(e.message).split("\n")[0]})`);
	process.exit(SKIP);
}
const BASE_SMTP = {
	smtp_enabled: true, smtp_host: "mail.example.com", smtp_port: 587, smtp_from: "alerts@example.com",
	smtp_user: "", smtp_use_tls: true, smtp_password_set: false,
};
const STUB = `<script>
window.__calls = [];
window.__errors = [];
window.apiFetch = (url, opts = {}) => new Promise((resolve) => {
	window.__calls.push({ url, method: opts.method || "GET", body: opts.body ? JSON.parse(opts.body) : null, signal: opts.signal, resolve });
});
window.justUpToast = { success() {}, error(message) { window.__errors.push(message); }, warning() {} };
</script>`;
const PAGE = (smtp) => `<!doctype html><html lang="en"><head><meta charset="utf-8"></head><body>${STUB}
<div id="settings-form"><div id="smtp-host">${render(smtp)}</div></div>
<script type="module">
import { initStorage, loadSettings } from "/static/js/settings.storage.js";
import { resetDomCache } from "/static/js/settings.core.js";
window.__storage = { initStorage, loadSettings, resetDomCache };
initStorage();
window.__ready = true;
</script></body></html>`;
let current = PAGE(BASE_SMTP);
const server = http.createServer((req, res) => {
	const url = new URL(req.url, "http://localhost");
	if (url.pathname === "/") { res.setHeader("Content-Type", "text/html"); res.end(current); return; }
	const file = path.normalize(path.join(ROOT, "app", url.pathname));
	if (!file.startsWith(STATIC + path.sep) || !existsSync(file)) { res.statusCode = 404; res.end(); return; }
	res.setHeader("Content-Type", path.extname(file) === ".js" ? "text/javascript" : "application/octet-stream");
	res.end(readFileSync(file));
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const base = `http://127.0.0.1:${server.address().port}/`;
const findings = [];
let checked = 0;
const missing = [];

// Resolve the n-th recorded apiFetch call with a response shaped like fetch's.
const respond = (page, index, success, body = {}) => page.evaluate(([i, good, payload]) => {
	window.__calls[i].resolve({ ok: good, status: good ? 200 : 500, json: async () => payload });
}, [index, success, body]);
const calls = (page) => page.evaluate(() => window.__calls.map((c) => ({ url: c.url, method: c.method, body: c.body })));
const waitForCalls = (page, count) => page.waitForFunction((expected) => window.__calls.length >= expected, count);
const waitForIdle = (page) => page.waitForFunction(() => window.justUpSettingsSavePending === null);
const waitForTls = (page, expected) => page.waitForFunction((wanted) => {
	const el = document.getElementById("smtp_use_tls");
	return el.checked === wanted.checked && (el.dataset.storedValue ?? null) === wanted.stored && (el.dataset.wantedValue ?? null) === wanted.wanted;
}, expected);
const tls = (page) => page.evaluate(() => {
	const el = document.getElementById("smtp_use_tls");
	return {
		checked: el.checked, disabled: el.disabled, stored: el.dataset.storedValue ?? null, wanted: el.dataset.wantedValue ?? null,
		label: document.querySelector('label[for="smtp_use_tls"]').textContent.trim(),
	};
});
const setPort = (page, value) => page.evaluate((v) => {
	const input = document.getElementById("smtp_port");
	input.value = v;
	input.dispatchEvent(new Event("input", { bubbles: true }));
}, value);
const loadPage = async (page, smtp) => {
	current = PAGE(smtp);
	await page.goto(base);
	await page.waitForFunction(() => window.__ready === true);
};

for (const name of engines) {
	let browser;
	try { browser = await pw[name].launch(); } catch (e) {
		console.log(`SKIP ${name}: ${String(e.message).split("\n")[0]}`);
		missing.push(name);
		continue;
	}
	const page = await (await browser.newContext()).newPage();
	const errors = [];
	page.on("pageerror", (e) => errors.push(String(e)));
	const check = (label, condition, detail = "") => { if (!condition) findings.push(`${name}: ${label}${detail ? ` (${detail})` : ""}`); };
	checked++;

	// 1. Default: switch on when the stored value is missing (attribute absent) and after init.
	await loadPage(page, BASE_SMTP);
	check("switch is on by default", (await tls(page)).checked === true);
	await page.evaluate(() => {
		const el = document.getElementById("smtp_use_tls");
		el.removeAttribute("data-stored-value");
		el.checked = false;
		window.__storage.initStorage();
	});
	check("switch is on when no stored value exists", (await tls(page)).checked === true);

	// 2. Stored false survives a GET /settings reload and an HTMX swap of the fragment.
	await loadPage(page, { ...BASE_SMTP, smtp_use_tls: false });
	let state = await tls(page);
	check("stored false renders as off", state.checked === false && state.stored === "false");
	const reload = page.evaluate(() => window.__storage.loadSettings());
	await respond(page, 0, true, { smtp_enabled: true, smtp_host: "mail.example.com", smtp_port: 587, smtp_from: "alerts@example.com", smtp_use_tls: false });
	await reload;
	check("stored false stays off after loadSettings()", (await tls(page)).checked === false);
	await page.evaluate((html) => {
		document.getElementById("smtp-host").innerHTML = html;
		document.body.dispatchEvent(new CustomEvent("htmx:afterSwap", { bubbles: true }));
		window.__storage.initStorage();
	}, render({ ...BASE_SMTP, smtp_use_tls: false }));
	check("stored false stays off after an HTMX swap", (await tls(page)).checked === false);

	// 3. Port 465 locks the switch on with the implicit TLS label; leaving 465 restores the stored state.
	await setPort(page, "465");
	state = await tls(page);
	check("port 465 locks the switch on", state.checked === true && state.disabled === true, JSON.stringify(state));
	check("port 465 shows the implicit TLS label", state.label === "Implicit TLS (SMTPS)", state.label);
	await setPort(page, "587");
	state = await tls(page);
	check("leaving 465 restores the stored off state", state.checked === false && state.disabled === false, JSON.stringify(state));
	check("leaving 465 restores the STARTTLS label", state.label === "Use STARTTLS", state.label);
	check("a port change alone sends no request", (await calls(page)).length === 1);

	// 4. Failed save rolls the switch back to the stored state.
	await loadPage(page, BASE_SMTP);
	await page.click("#smtp_use_tls");
	await waitForCalls(page, 1);
	check("toggle shows the wanted state while saving", (await tls(page)).checked === false);
	await respond(page, 0, false);
	await waitForTls(page, { checked: true, stored: "true", wanted: null });
	state = await tls(page);
	check("failed save rolls back to on", state.checked === true && state.stored === "true" && state.wanted === null, JSON.stringify(state));

	// 5. loadSettings() leaves SMTP fields alone when the response lacks them.
	await loadPage(page, { ...BASE_SMTP, smtp_user: "alerts", smtp_use_tls: false });
	const partial = page.evaluate(() => window.__storage.loadSettings());
	await respond(page, 0, true, { signal_enabled: false });
	await partial;
	const kept = await page.evaluate(() => ({
		host: document.getElementById("smtp_host").value, port: document.getElementById("smtp_port").value,
		from: document.getElementById("smtp_from").value, user: document.getElementById("smtp_user").value,
		enabled: document.getElementById("smtp_enabled").checked, tls: document.getElementById("smtp_use_tls").checked,
	}));
	check("loadSettings() keeps the SMTP fields when the response lacks them",
		kept.host === "mail.example.com" && kept.port === "587" && kept.from === "alerts@example.com" &&
		kept.user === "alerts" && kept.enabled === true && kept.tls === false, JSON.stringify(kept));

	// 6. Race: a quick double toggle is saved in order and the UI ends on the last choice.
	await loadPage(page, BASE_SMTP);
	await page.click("#smtp_use_tls"); // off
	await page.click("#smtp_use_tls"); // on again
	await waitForCalls(page, 1);
	let sent = await calls(page);
	check("second save waits for the first (one request in flight)", sent.length === 1, `requests=${sent.length}`);
	check("pending saves are published for the test-mail button", await page.evaluate(() => window.justUpSettingsSavePending !== null));
	await respond(page, 0, true);
	await waitForCalls(page, 2);
	sent = await calls(page);
	check("saves go out in click order", sent.length === 2 && sent[0].body.smtp_use_tls === false && sent[1].body.smtp_use_tls === true, JSON.stringify(sent.map((c) => c.body)));
	await respond(page, 1, true);
	await waitForIdle(page);
	state = await tls(page);
	check("double toggle ends on the last choice", state.checked === true && state.stored === "true" && state.wanted === null, JSON.stringify(state));
	check("no save is pending afterwards", await page.evaluate(() => window.justUpSettingsSavePending === null));

	// 6b. Race: first save fails, second succeeds; stored state follows the server, not the failed save.
	await loadPage(page, BASE_SMTP);
	await page.click("#smtp_use_tls"); // off
	await page.click("#smtp_use_tls"); // on
	await waitForCalls(page, 1);
	await respond(page, 0, false);
	await waitForCalls(page, 2);
	await respond(page, 1, true);
	await waitForIdle(page);
	state = await tls(page);
	check("failed first save does not roll back the successful second", state.checked === true && state.stored === "true", JSON.stringify(state));

	// 6c. Toggle to off, then second save fails: switch returns to the last confirmed value (off was confirmed).
	await loadPage(page, BASE_SMTP);
	await page.click("#smtp_use_tls"); // off
	await waitForCalls(page, 1);
	await respond(page, 0, true);
	await waitForIdle(page);
	await page.click("#smtp_use_tls"); // on
	await waitForCalls(page, 2);
	await respond(page, 1, false);
	await waitForTls(page, { checked: false, stored: "false", wanted: null });
	state = await tls(page);
	check("failure of the newest save returns to the last confirmed value", state.checked === false && state.stored === "false", JSON.stringify(state));

	// 6d. Race: port change to 465 and back while a save is pending keeps the wanted choice.
	await loadPage(page, BASE_SMTP);
	await page.click("#smtp_use_tls"); // off, request pending
	await waitForCalls(page, 1);
	await setPort(page, "465");
	check("port 465 during a pending save locks the switch on", (await tls(page)).checked === true);
	await setPort(page, "587");
	state = await tls(page);
	check("leaving 465 during a pending save keeps the wanted choice", state.checked === false && state.stored === "true", JSON.stringify(state));
	await respond(page, 0, true);
	await waitForIdle(page);
	state = await tls(page);
	check("the confirmed choice is shown after the pending save", state.checked === false && state.stored === "false" && state.wanted === null, JSON.stringify(state));

	// 6e. Same-key saves from other fields keep their order too (host edited twice).
	await loadPage(page, BASE_SMTP);
	await page.evaluate(() => {
		const host = document.getElementById("smtp_host");
		for (const value of ["first.example.com", "second.example.com"]) {
			host.focus();
			host.value = value;
			host.blur();
		}
	});
	await waitForCalls(page, 1);
	sent = await calls(page);
	check("second host save waits for the first", sent.length === 1 && sent[0].body.smtp_host === "first.example.com", JSON.stringify(sent.map((c) => c.body)));
	await respond(page, 0, true);
	await waitForCalls(page, 2);
	sent = await calls(page);
	check("host saves keep their order", sent.length === 2 && sent[1].body.smtp_host === "second.example.com", JSON.stringify(sent.map((c) => c.body)));

	// An older preview response must not replace the variant selected most recently.
	await loadPage(page, BASE_SMTP);
	const previewStart = (await calls(page)).length;
	await page.addScriptTag({ url: "/static/js/settings.smtp-template.js" });
	await page.evaluate(() => document.dispatchEvent(new Event("DOMContentLoaded")));
	await waitForCalls(page, previewStart + 1);
	await page.click("#btn-preview-resolved");
	await waitForCalls(page, previewStart + 2);
	const previews = await page.evaluate(() => window.__calls.map((call, index) => ({ index, url: call.url, body: call.body, aborted: call.signal?.aborted }))
		.filter((call) => call.url === "/settings/smtp/template/preview"));
	check("the previous preview request is aborted", previews[0]?.aborted === true);
	await respond(page, previews[1].index, true, { html: "<html><body>resolved-preview</body></html>" });
	await page.waitForFunction(() => document.getElementById("email_preview_frame").srcdoc.includes("resolved-preview"));
	await respond(page, previews[0].index, true, { html: "<html><body>down-preview</body></html>" });
	check("a late preview cannot replace the last selected variant",
		await page.locator("#email_preview_frame").evaluate((frame) => frame.srcdoc.includes("resolved-preview")));

	// Do not queue a test email after any autosave failed.
	await loadPage(page, BASE_SMTP);
	await page.addScriptTag({ url: "/static/js/settings.users.js" });
	await page.evaluate(() => document.dispatchEvent(new Event("DOMContentLoaded")));
	await page.click("#smtp_use_tls");
	await waitForCalls(page, 1);
	await page.locator("#smtp_test_recipient").fill("test@example.com");
	await page.evaluate(() => { void window.SettingsPage.Users.testSmtp(); });
	await respond(page, 0, false);
	await page.waitForFunction(() => !document.getElementById("test-smtp-btn").disabled);
	const testMailCalls = (await calls(page)).filter((call) => call.url === "/settings/smtp/test-mail");
	const saveError = await page.evaluate(() => window.__errors.includes("Could not save settings. Test email was not sent."));
	check("a failed setting save prevents the test-mail request", testMailCalls.length === 0);
	check("the failed save is reported and the test button is re-enabled", saveError);

	if (errors.length) findings.push(`${name}: page errors: ${errors.join(" | ")}`);
	console.log(`${name}: SMTP switch scenarios checked`);
	await browser.close();
}
server.close();
if (findings.length) { console.log(findings.join("\n")); process.exit(1); }
if (missing.length || !checked) { console.log(`SKIP: incomplete browser matrix${missing.length ? `; unavailable: ${missing.join(", ")}` : ""}`); process.exit(SKIP); }
console.log(`OK: ${checked} engine run(s), 0 findings`);
