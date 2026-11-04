//
// tests/layout/auth.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { test as base } from "@playwright/test";
import { startApp } from "./harness.mjs";

const test = base.extend({
	authApp: [async ({}, use) => {
		const app = await startApp();
		try {
			await use(app);
		} finally {
			await app.stop();
		}
	}, { scope: "worker", timeout: 90_000 }],
});

test("cookie-only authentication enforces CSRF and revokes logout sessions @auth", async ({ page, browserName, authApp }) => {
	const authHeaders = [];
	page.on("request", (request) => {
		if (request.headers().authorization) authHeaders.push(request.url());
	});
	const next = "Auth-Flow-Check-2026!";
	await page.goto(`${authApp.base}/ui/login`);
	await page.fill("#username", authApp.bootstrap.username);
	await page.fill("#password", authApp.bootstrap.password);
	await page.click("#login-form button[type=submit]");
	await page.waitForURL(/change-password/);
	await page.fill("#new-password", next);
	await page.fill("#confirm-password", next);
	await page.click("#change-password-form button[type=submit]");
	await page.waitForURL(/login/, { timeout: 15000 });
	await page.fill("#username", authApp.bootstrap.username);
	await page.fill("#password", next);
	await page.click("#login-form button[type=submit]");
	await page.waitForURL((url) => !/login|change-password/.test(url.pathname), { timeout: 15000 });

	assert.equal(await page.evaluate(() => localStorage.getItem("token")), null, `${browserName}: no token in localStorage`);
	assert.ok(await page.evaluate(() => Object.keys(localStorage).every((key) => !/token/i.test(key))));
	const context = page.context();
	const cookies = await context.cookies();
	const session = cookies.find((cookie) => cookie.name === "token" || cookie.name === "__Host-token");
	assert.ok(session?.httpOnly && session.sameSite === "Lax" && session.path === "/", `${browserName}: session cookie flags`);
	assert.ok(await page.evaluate(() => !/(^|;\s*)(__Host-)?token=/.test(document.cookie)), `${browserName}: session cookie is not script-readable`);
	const csrf = cookies.find((cookie) => cookie.name === "csrf_token");
	assert.ok(csrf && csrf.expires - Date.now() / 1000 > 6.9 * 86400, `${browserName}: CSRF cookie lifetime is at least seven days`);

	assert.equal(await page.evaluate(async () => (await window.apiFetch("/me")).status), 200, "GET /me uses the session cookie");
	const write = (headers) => page.evaluate(async (requestHeaders) => (await fetch("/me/theme", {
		method: "PATCH",
		headers: { "Content-Type": "application/json", ...requestHeaders },
		body: JSON.stringify({ theme: "dark" }),
	})).status, headers);
	assert.equal(await write({}), 403, "cookie write without CSRF is rejected");
	assert.equal(await write({ "X-CSRF-Token": "wrong" }), 403, "cookie write with invalid CSRF is rejected");
	const validCsrf = await page.evaluate(async () => (await window.apiFetch("/me/theme", {
		method: "PATCH",
		headers: { "Content-Type": "application/json" },
		body: JSON.stringify({ theme: "dark" }),
	})).status);
	assert.ok(validCsrf >= 200 && validCsrf < 300, `cookie write with valid CSRF returns 2xx, got ${validCsrf}`);
	const badBearer = await page.evaluate(async () => (await fetch("/me", { headers: { Authorization: "Bearer invalid" } })).status);
	assert.equal(badBearer, 401, "invalid bearer is rejected even with a valid cookie");
	authHeaders.length = 0;

	const sse = await page.evaluate(() => new Promise((resolve) => {
		const events = new EventSource("/ui/events");
		const timer = setTimeout(() => { events.close(); resolve("timeout"); }, 8000);
		events.onopen = () => { clearTimeout(timer); events.close(); resolve("open"); };
		events.onerror = () => { clearTimeout(timer); events.close(); resolve("error"); };
	}));
	assert.equal(sse, "open", "SSE opens with the session cookie");
	assert.deepEqual(authHeaders, [], "browser UI sends no Authorization header");
	assert.equal(await page.evaluate(async () => (await fetch("/csp-report", {
		method: "POST",
		headers: { "Content-Type": "application/csp-report" },
		body: "{}",
	})).status), 204, "CSP report accepts a cookie-carrying report without CSRF");
	assert.equal(await page.evaluate(async () => (await window.apiFetch("/users")).status), 200, "admin route accepts the session cookie");

	const tokenBeforeLogout = session.value;
	await page.evaluate(() => document.getElementById("logout-btn")?.click());
	await page.waitForURL(/login/, { timeout: 15000 });
	assert.equal((await context.cookies()).some((cookie) => cookie.name === "token" || cookie.name === "__Host-token"), false, "logout removes the session cookie");
	const revoked = await context.request.get(`${authApp.base}/me`, { headers: { Authorization: `Bearer ${tokenBeforeLogout}` } });
	assert.equal(revoked.status(), 401, "logout revokes the session server-side");
	assert.equal(await page.evaluate(async () => (await fetch("/me")).status), 401, "anonymous /me is rejected after logout");
});