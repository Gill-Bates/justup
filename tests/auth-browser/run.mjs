//
// tests/auth-browser/run.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Browser regression check for the cookie-only sign-in: HttpOnly session cookie, no token in
// localStorage, CSRF + Origin on cookie writes, no bearer fallback, SSE and logout with cookies.
// It starts the app on a free port with a throw-away data directory (like tests/layout/run.mjs).
//
//   node tests/auth-browser/run.mjs [--browsers chromium,webkit,firefox]
//
// Playwright is a dev-only tool resolved from the working directory, then $PLAYWRIGHT_DIR;
// $PYTHON selects the interpreter that has the app dependencies (default python3).
// Exit codes: 0 = clean, 1 = findings, 77 = skipped (Playwright or an engine is missing).
import { spawn, spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import net from "node:net";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const SKIP = 77;
const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
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
const childIdx = process.argv.indexOf("--engine");
if (childIdx < 0) {
	// The bootstrap password is single-use, so every engine gets its own app instance in a child run.
	const codes = engines.map((name) => {
		console.log(`== ${name}`);
		return spawnSync(process.execPath, [fileURLToPath(import.meta.url), "--engine", name], { stdio: "inherit", env: process.env }).status;
	});
	if (codes.includes(1)) process.exit(1);
	process.exit(codes.every((code) => code === SKIP) ? SKIP : 0);
}
const ENGINE = process.argv[childIdx + 1];
const data = mkdtempSync(path.join(tmpdir(), "justup-auth-browser-"));
const port = await new Promise((r) => { const s = net.createServer().listen(0, "127.0.0.1", () => { const p = s.address().port; s.close(() => r(p)); }); });
const env = {
	...process.env,
	SECRET_KEY: Buffer.from(Array.from({ length: 32 }, () => Math.floor(Math.random() * 256))).toString("base64"),
	JUSTUP_DATA_DIR: data, JUSTUP_HOST: "127.0.0.1", JUSTUP_PORT: String(port), LOG_LEVEL: "WARNING",
	HTTPS_PROXY: "http://127.0.0.1:9", HTTP_PROXY: "http://127.0.0.1:9", NO_PROXY: "127.0.0.1,localhost",
};
const proc = spawn(process.env.PYTHON || "python3", [path.join(repo, "tests/layout/app_stub.py")], { cwd: repo, env, stdio: "ignore" });
const base = `http://127.0.0.1:${port}`;
const results = [];
const check = (name, ok, extra = "") => { results.push(`${ok ? "PASS" : "FAIL"} ${name} ${extra}`); };
let browser;
const cleanup = async () => { try { await browser?.close(); } catch {} proc.kill("SIGKILL"); rmSync(data, { recursive: true, force: true }); };
process.on("SIGTERM", async () => { await cleanup(); process.exit(143); });
try {
	for (let i = 0; i < 600; i++) { try { if ((await fetch(`${base}/ui/login`)).ok) break; } catch {} await new Promise((r) => setTimeout(r, 100)); }
	const boot = JSON.parse(readFileSync(path.join(data, ".bootstrap-admin-credentials"), "utf8"));
	const engine = ENGINE;
	try { browser = await pw[engine].launch(); } catch (e) { console.log(`SKIP ${engine}: ${String(e.message).split("\n")[0]}`); await cleanup(); process.exit(SKIP); }
	const ctx = await browser.newContext();
	const page = await ctx.newPage();
	const authHeaders = [];
	page.on("request", (r) => { const h = r.headers(); if (h.authorization) authHeaders.push(r.url()); });
	const next = "Auth-Flow-Check-2026!";
	await page.goto(`${base}/ui/login`);
	await page.fill("#username", boot.username); await page.fill("#password", boot.password);
	await page.click("#login-form button[type=submit]");
	await page.waitForURL(/change-password/);
	await page.fill("#new-password", next); await page.fill("#confirm-password", next);
	await page.click("#change-password-form button[type=submit]");
	await page.waitForURL(/login/, { timeout: 15000 });
	await page.fill("#username", boot.username); await page.fill("#password", next);
	await page.click("#login-form button[type=submit]");
	await page.waitForURL((u) => !/login|change-password/.test(u.pathname), { timeout: 15000 });
	check("login via UI", true);
	check("no token in localStorage", await page.evaluate(() => localStorage.getItem("token") === null && Object.keys(localStorage).every((k) => !/token/i.test(k))));
	const cookies = await ctx.cookies();
	const sess = cookies.find((c) => c.name === "token" || c.name === "__Host-token");
	check("session cookie HttpOnly, SameSite=Lax, path /", !!sess && sess.httpOnly && sess.sameSite === "Lax" && sess.path === "/", JSON.stringify({ name: sess?.name, secure: sess?.secure }));
	check("session cookie not readable by script", await page.evaluate(() => !/(^|;\s*)(__Host-)?token=/.test(document.cookie)));
	const csrf = cookies.find((c) => c.name === "csrf_token");
	check("csrf cookie lifetime >= 7 days", !!csrf && csrf.expires - Date.now() / 1000 > 6.9 * 86400, `expires in ${csrf ? Math.round((csrf.expires - Date.now() / 1000) / 3600) : "?"} h`);
	const me = await page.evaluate(async () => (await window.apiFetch("/me")).status);
	check("apiFetch GET /me with cookie only", me === 200, `status ${me}`);
	const noCsrf = await page.evaluate(async () => (await fetch("/me/theme", { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ theme: "dark" }) })).status);
	check("cookie write without CSRF header -> 403", noCsrf === 403, `status ${noCsrf}`);
	const badCsrf = await page.evaluate(async () => (await fetch("/me/theme", { method: "PATCH", headers: { "Content-Type": "application/json", "X-CSRF-Token": "wrong" }, body: JSON.stringify({ theme: "dark" }) })).status);
	check("cookie write with wrong CSRF token -> 403", badCsrf === 403, `status ${badCsrf}`);
	const okWrite = await page.evaluate(async () => (await window.apiFetch("/me/theme", { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ theme: "dark" }) })).status);
	check("cookie write with valid CSRF -> 2xx", okWrite >= 200 && okWrite < 300, `status ${okWrite}`);
	const badBearer = await page.evaluate(async () => (await fetch("/me", { headers: { Authorization: "Bearer invalid" } })).status);
	authHeaders.length = 0;
	check("invalid bearer + valid cookie -> 401", badBearer === 401, `status ${badBearer}`);
	const sse = await page.evaluate(() => new Promise((resolve) => {
		const es = new EventSource("/ui/events");
		const t = setTimeout(() => { es.close(); resolve("timeout"); }, 8000);
		es.onopen = () => { clearTimeout(t); es.close(); resolve("open"); };
		es.onerror = () => { clearTimeout(t); es.close(); resolve("error"); };
	}));
	check("SSE /ui/events opens with cookie", sse === "open", sse);
	check("no Authorization header sent by the browser UI", authHeaders.length === 0, authHeaders.join(","));
	// Browsers send CSP reports with the session cookie but never with an X-CSRF-Token header.
	const report = await page.evaluate(async () => (await fetch("/csp-report", { method: "POST", headers: { "Content-Type": "application/csp-report" }, body: "{}" })).status);
	check("CSP report sink accepts a cookie-carrying report without CSRF", report === 204, `status ${report}`);
	const adminList = await page.evaluate(async () => (await window.apiFetch("/users")).status);
	check("admin route /users with cookie", adminList === 200, `status ${adminList}`);
	// Logout through the UI button (htmx) and check server-side revocation + cookie removal.
	const tokenBefore = sess.value;
	await page.evaluate(() => document.getElementById("logout-btn")?.click());
	await page.waitForURL(/login/, { timeout: 15000 });
	const after = (await ctx.cookies()).find((c) => c.name === "token" || c.name === "__Host-token");
	check("logout removes session cookie", !after);
	const revoked = await page.request.get(`${base}/me`, { headers: { Authorization: `Bearer ${tokenBefore}` } });
	check("logout revoked the token server-side", revoked.status() === 401, `status ${revoked.status()}`);
	const anon = await page.evaluate(async () => (await fetch("/me")).status);
	check("after logout /me -> 401", anon === 401, `status ${anon}`);
} catch (e) {
	results.push(`ERROR ${String(e.message).split("\n")[0]}`);
} finally {
	await cleanup();
}
console.log(results.join("\n"));
process.exit(results.some((r) => r.startsWith("FAIL") || r.startsWith("ERROR")) ? 1 : 0);
