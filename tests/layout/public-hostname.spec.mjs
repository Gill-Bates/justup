//
// tests/layout/public-hostname.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import { execFileSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test } from "@playwright/test";
import { resolvePythonExecutable, startStaticAppServer } from "./test-support.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const PYTHON = resolvePythonExecutable(ROOT);
const RENDER = `
import json, sys
from jinja2 import Environment, FileSystemLoader, select_autoescape
env = Environment(loader=FileSystemLoader(sys.argv[1]), autoescape=select_autoescape(["html"]))
print(env.get_template("settings/general.html").render(
    settings=json.loads(sys.argv[2]),
    settings_limits={"metrics_timeout_minutes": {"min": 1, "max": 60}}))
`;
const BASE = { use_utc_dashboard: false, pdf_page_size: "a4", metrics_timeout_minutes: 5, public_hostname: "" };
const STUB = `<script>
window.__calls = [];
window.__toasts = [];
window.apiFetch = (url, opts = {}) => new Promise((resolve) => {
    window.__calls.push({ url, method: opts.method || "GET", body: opts.body ? JSON.parse(opts.body) : null, resolve });
});
window.justUpToast = {
    success(message) { window.__toasts.push(["success", message]); },
    error(message) { window.__toasts.push(["error", message]); },
    warning(message) { window.__toasts.push(["warning", message]); },
};
window.JuFieldErrors = { clear() {}, show() {} };
</script>`;
const PAGE = (settings) => `<!doctype html><html lang="en"><head><meta charset="utf-8"></head><body>${STUB}
<div id="settings-form"><div id="general-host">${render(settings)}</div></div>
<script type="module">
import { initStorage } from "/static/js/settings.storage.js";
initStorage();
window.__ready = true;
</script></body></html>`;

function render(settings) {
	return execFileSync(PYTHON, ["-c", RENDER, path.join(ROOT, "app", "templates"), JSON.stringify(settings)], { encoding: "utf8" });
}

let current = "";
let server;
let baseUrl;
let browserErrors = [];
const respond = (page, index, status, body) => page.evaluate(([i, code, payload]) => {
	window.__calls[i].resolve({ ok: code < 400, status: code, json: async () => payload });
}, [index, status, body]);
const calls = (page) => page.evaluate(() => window.__calls.map((call) => ({ url: call.url, method: call.method, body: call.body })));
const toasts = (page) => page.evaluate(() => window.__toasts);
const waitForCalls = (page, count) => expect.poll(async () => (await calls(page)).length).toBeGreaterThanOrEqual(count);
const hintHidden = (page) => page.locator("#public_hostname_missing_hint").evaluate((el) => el.hidden);
const loadPage = async (page, settings) => {
	current = PAGE(settings);
	await page.goto(baseUrl);
	await page.waitForFunction(() => window.__ready === true);
};

test.describe("Public hostname field in Settings > General @public-hostname-ui", () => {
	test.beforeAll(async () => {
		current = PAGE(BASE);
		server = await startStaticAppServer({ root: ROOT, renderPage: () => current });
		baseUrl = server.baseUrl;
	});
	test.afterAll(async () => {
		if (server) await server.close();
	});
	test.beforeEach(({ page }) => {
		browserErrors = [];
		page.on("pageerror", (error) => browserErrors.push(String(error)));
	});
	test.afterEach(() => expect(browserErrors).toEqual([]));

	test("empty field shows the hint and an accessible detect button", async ({ page }) => {
		await loadPage(page, BASE);
		const button = page.locator("#public_hostname_detect");
		await expect(button).toHaveAttribute("aria-label", "Auto-detect from current address");
		await expect(button).toHaveAttribute("title", "Auto-detect from current address");
		await expect(page.locator("label[for=public_hostname]")).toHaveText("Public hostname");
		await expect(page.locator("#public_hostname_hint")).toContainText("Hostname or IP only");
		expect(await hintHidden(page)).toBe(false);
	});

	test("stored value hides the hint", async ({ page }) => {
		await loadPage(page, { ...BASE, public_hostname: "justup.example.com" });
		await expect(page.locator("#public_hostname")).toHaveValue("justup.example.com");
		expect(await hintHidden(page)).toBe(true);
	});

	test("detect fills the current hostname, saves it and confirms with a toast", async ({ page }) => {
		await loadPage(page, BASE);
		const expected = new URL(baseUrl).hostname;
		await page.locator("#public_hostname_detect").click();
		await expect(page.locator("#public_hostname")).toHaveValue(expected);
		await waitForCalls(page, 1);
		expect((await calls(page))[0]).toMatchObject({ method: "PATCH", body: { public_hostname: expected } });
		await respond(page, 0, 200, { public_hostname: expected });
		await expect.poll(() => toasts(page)).toContainEqual(["success", "Public hostname updated"]);
		expect(await hintHidden(page)).toBe(true);
	});

	test("an invalid value shows the server message and keeps the hint logic intact", async ({ page }) => {
		await loadPage(page, BASE);
		await page.locator("#public_hostname").fill("https://justup.example.com");
		await page.locator("#public_hostname").blur();
		await waitForCalls(page, 1);
		expect((await calls(page))[0].body).toEqual({ public_hostname: "https://justup.example.com" });
		await respond(page, 0, 422, { detail: "public_hostname: Enter a hostname or IP only - no scheme, port or path" });
		await expect.poll(() => toasts(page)).toContainEqual([
			"error", "public_hostname: Enter a hostname or IP only - no scheme, port or path",
		]);
		expect(await hintHidden(page)).toBe(false);
	});

	test("a late reply does not overwrite a value typed after the request was sent", async ({ page }) => {
		await loadPage(page, BASE);
		const field = page.locator("#public_hostname");
		await field.fill("monitor.example.com");
		await field.blur();
		await waitForCalls(page, 1);
		await field.fill("justup.example.com");
		await respond(page, 0, 200, { public_hostname: "monitor.example.com" });
		await expect.poll(() => toasts(page)).toContainEqual(["success", "Public hostname updated"]);
		await expect(field).toHaveValue("justup.example.com");
		await field.blur();
		await waitForCalls(page, 2);
		expect((await calls(page))[1].body).toEqual({ public_hostname: "justup.example.com" });
	});
	test("detect warns when the page is not opened via https on the default port", async ({ page }) => {
		await loadPage(page, BASE);
		await page.locator("#public_hostname_detect").click();
		await waitForCalls(page, 1);
		await respond(page, 0, 200, { public_hostname: new URL(baseUrl).hostname });
		await expect.poll(async () => (await toasts(page)).filter(([kind]) => kind === "warning").length).toBe(1);
		expect((await toasts(page)).find(([kind]) => kind === "warning")[1]).toContain("without a port");
	});
	test("clearing the field saves an empty value and shows the hint again", async ({ page }) => {
		await loadPage(page, { ...BASE, public_hostname: "justup.example.com" });
		await page.locator("#public_hostname").fill("");
		await page.locator("#public_hostname").blur();
		await waitForCalls(page, 1);
		expect((await calls(page))[0].body).toEqual({ public_hostname: "" });
		await respond(page, 0, 200, { public_hostname: "" });
		await expect.poll(() => hintHidden(page)).toBe(false);
	});
});
