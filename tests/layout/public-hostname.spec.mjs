//
// tests/layout/public-hostname.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test } from "@playwright/test";
import { renderTemplates, startStaticAppServer } from "./test-support.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const BASE = { use_utc_dashboard: false, pdf_page_size: "a4", pdf_export_enabled: true, public_hostname: "", auth_required: true, rate_limit_auth: "10/minute", session_max_days: 1 };
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
</script>
<script src="/static/js/utils/field-errors.js"></script>`;
const PAGE = (settings) => `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<link href="/static/css/bootstrap.min.css" rel="stylesheet"><link href="/static/css/material-icons.css" rel="stylesheet"><link href="/static/css/gentelella-layout.css" rel="stylesheet"><link href="/static/css/ui.css" rel="stylesheet"></head><body>${STUB}
<div id="settings-form"><div id="general-host" style="container: ju-page / inline-size">${render(settings)}</div></div>
<script src="/static/js/bootstrap.bundle.min.js"></script>
<script type="module">
import { initStorage } from "/static/js/settings.storage.js";
initStorage();
window.__ready = true;
</script></body></html>`;

function render(settings) {
	return renderTemplates(ROOT, [
		{ template: "settings/general.html", context: { settings, settings_limits: { session_max_days: { min: 1, max: 7 } } } },
		{ template: "settings/modals.html" },
	]);
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

	test("field shows its hint and an accessible detect button", async ({ page }) => {
		await loadPage(page, BASE);
		const button = page.locator("#public_hostname_detect");
		await expect(button).toHaveAttribute("aria-label", "Auto-detect from current address");
		await expect(button).toHaveAttribute("title", "Auto-detect from current address");
		await expect(page.locator("label[for=public_hostname]")).toHaveText("Public hostname");
		await expect(page.locator("#public_hostname_hint")).toContainText("Hostname or IP only");
	});

	test("the removed global down alert delay slider is not rendered", async ({ page }) => {
		await loadPage(page, BASE);
		await expect(page.locator("#metrics_timeout_minutes")).toHaveCount(0);
		await expect(page.getByText("Down Alert Delay")).toHaveCount(0);
	});
	test("stored value is shown in the field", async ({ page }) => {
		await loadPage(page, { ...BASE, public_hostname: "justup.example.com" });
		await expect(page.locator("#public_hostname")).toHaveValue("justup.example.com");
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
	});

	test("an invalid value shows the server message", async ({ page }) => {
		await loadPage(page, BASE);
		await page.locator("#public_hostname").fill("https://justup.example.com");
		await page.locator("#public_hostname").blur();
		await waitForCalls(page, 1);
		expect((await calls(page))[0].body).toEqual({ public_hostname: "https://justup.example.com" });
		await respond(page, 0, 422, { detail: "public_hostname: Enter a hostname or IP only - no scheme, port or path" });
		await expect.poll(() => toasts(page)).toContainEqual([
			"error", "public_hostname: Enter a hostname or IP only - no scheme, port or path",
		]);
		// DC-FORM-12: the field error persists after the toast and clears on edit.
		const field = page.locator("#public_hostname");
		await expect(field).toHaveAttribute("aria-invalid", "true");
		await expect(page.locator("#public_hostname-field-error")).toContainText("Enter a hostname or IP only");
		await field.pressSequentially("x");
		await expect(field).not.toHaveAttribute("aria-invalid", "true");
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
	test("clearing the field saves an empty value", async ({ page }) => {
		await loadPage(page, { ...BASE, public_hostname: "justup.example.com" });
		await page.locator("#public_hostname").fill("");
		await page.locator("#public_hostname").blur();
		await waitForCalls(page, 1);
		expect((await calls(page))[0].body).toEqual({ public_hostname: "" });
		await respond(page, 0, 200, { public_hostname: "" });
		await expect(page.locator("#public_hostname")).toHaveValue("");
	});
	test("PDF card is the third card, holds the switch and the paper size, and saves like the others", async ({ page }) => {
		await loadPage(page, BASE);
		const titles = page.locator("#general-settings-form .settings-card .card-header h2");
		await expect(titles).toHaveText(["Display", "Instance", "PDF", "Access & Sessions"]);
		const card = page.locator(".settings-card", { has: page.locator("#pdf_export_enabled") });
		await expect(card.locator("h2")).toHaveText("PDF");
		await expect(card.locator("#pdf_page_size")).toBeVisible();
		await expect(page.locator(".settings-card", { has: page.locator("#use_utc_dashboard") }).locator("#pdf_page_size")).toHaveCount(0);
		await expect(page.getByRole("switch", { name: "Enable PDF Export" })).toBeChecked();
		await page.getByRole("switch", { name: "Enable PDF Export" }).uncheck();
		await waitForCalls(page, 1);
		expect((await calls(page))[0]).toMatchObject({ method: "PATCH", body: { pdf_export_enabled: false } });
		// The paper size stays selectable and saves while export is off.
		await expect(page.locator("#pdf_page_size")).toBeEnabled();
		await page.locator("#pdf_page_size").selectOption("letter");
		await waitForCalls(page, 2);
		expect((await calls(page))[1].body).toEqual({ pdf_page_size: "letter" });
	});
	test("stored off state renders the switch off and the paper size enabled", async ({ page }) => {
		await loadPage(page, { ...BASE, pdf_export_enabled: false });
		await expect(page.locator("#pdf_export_enabled")).not.toBeChecked();
		await expect(page.locator("#pdf_page_size")).toBeEnabled();
	});
	test("Access & Sessions card sits on General with Require Authentication ON and no warning", async ({ page }) => {
		await loadPage(page, BASE);
		const card = page.locator(".settings-card", { has: page.locator("#auth_required") });
		await expect(card.locator("h2")).toHaveText("Access & Sessions");
		await expect(card.locator(".card-header button, .card-header [data-bs-toggle]")).toHaveCount(0);
		const toggle = page.getByRole("switch", { name: "Require Authentication" });
		await expect(toggle).toBeChecked();
		await expect(toggle).toHaveAttribute("aria-describedby", "auth_required_hint");
		await expect(page.locator("#auth_required_hint")).toHaveAttribute("role", "status");
		await expect(page.getByText("Your instance is publicly accessible!")).toBeHidden();
		await expect(card.locator("#rate_limit_auth_hint")).toHaveText("Per IP: N/second, minute, hour or day.");
		await expect(card.locator("#session_max_days_hint")).toHaveText("Applies to new logins only.");
	});
	test("stored OFF shows the warning on load and the switch off", async ({ page }) => {
		await loadPage(page, { ...BASE, auth_required: false });
		await expect(page.getByRole("switch", { name: "Require Authentication" })).not.toBeChecked();
		await expect(page.locator("#auth_required_hint")).toContainText("Your instance is publicly accessible!");
		await expect(page.getByText("Your instance is publicly accessible!")).toBeVisible();
	});
	test("switching OFF asks for the password, then shows the warning; cancel keeps ON", async ({ page }) => {
		await loadPage(page, BASE);
		const toggle = page.getByRole("switch", { name: "Require Authentication" });
		await toggle.click();
		await expect(page.locator("#authToggleModal")).toBeVisible();
		await expect(toggle).toBeChecked(); // reverted until confirmed
		await page.locator("#authToggleModal [data-bs-dismiss=modal]").first().click();
		await expect(page.locator("#authToggleModal")).toBeHidden();
		await expect(toggle).toBeChecked();
		await expect(page.getByText("Your instance is publicly accessible!")).toBeHidden();
		expect(await calls(page)).toHaveLength(0);
		await toggle.click();
		await page.locator("#auth-toggle-password").fill("secret-pw");
		await page.locator("#confirm-auth-toggle-btn").click();
		await waitForCalls(page, 1);
		expect((await calls(page))[0]).toMatchObject({ url: "/settings/auth/toggle", method: "POST", body: { password: "secret-pw", disable: true } });
		await respond(page, 0, 401, { detail: "Invalid password" });
		await expect(toggle).toBeChecked();
		await expect(page.getByText("Your instance is publicly accessible!")).toBeHidden();
		await page.locator("#auth-toggle-password").fill("right-pw");
		await page.locator("#confirm-auth-toggle-btn").click();
		await waitForCalls(page, 2);
		await respond(page, 1, 200, {});
		await expect(toggle).not.toBeChecked();
		await expect(page.getByText("Your instance is publicly accessible!")).toBeVisible();
		await expect(page.locator("#authToggleModal")).toBeHidden();
		// Back ON hides the warning again.
		await toggle.click();
		await page.locator("#auth-toggle-password").fill("right-pw");
		await page.locator("#confirm-auth-toggle-btn").click();
		await waitForCalls(page, 3);
		expect((await calls(page))[2].body).toMatchObject({ disable: false });
		await respond(page, 2, 200, {});
		await expect(toggle).toBeChecked();
		await expect(page.getByText("Your instance is publicly accessible!")).toBeHidden();
	});
	test("Login Rate Limit and Session Lifetime autosave on General", async ({ page }) => {
		await loadPage(page, BASE);
		await page.locator("#rate_limit_auth").fill("20/minute");
		await page.locator("#rate_limit_auth").blur();
		await waitForCalls(page, 1);
		expect((await calls(page))[0]).toMatchObject({ method: "PATCH", body: { rate_limit_auth: "20/minute" } });
		await respond(page, 0, 200, { rate_limit_auth: "20/minute" });
		await page.locator("#rate_limit_auth").fill("bogus");
		await page.locator("#rate_limit_auth").blur();
		await page.locator("#session_max_days").fill("3");
		await waitForCalls(page, 2);
		expect((await calls(page))[1]).toMatchObject({ method: "PATCH", body: { session_max_days: 3 } });
		expect(await calls(page)).toHaveLength(2); // the invalid rate limit was not sent
		await expect(page.locator("#session_max_days_value")).toHaveText("3 days");
	});
	for (const [name, width, height] of [["1440", 1440, 900], ["1024", 1024, 768], ["390", 390, 844]]) {
		test(`four cards fit at ${name}px without horizontal overflow`, async ({ page }) => {
			await page.setViewportSize({ width, height });
			await loadPage(page, BASE);
			const boxes = await page.locator("#general-settings-form .settings-card").evaluateAll((els) => els.map((el) => {
				const r = el.getBoundingClientRect();
				return { x: r.x, y: r.y, w: r.width, h: r.height };
			}));
			expect(boxes).toHaveLength(4);
			expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
			if (width > 640) {
				expect(boxes[1].x).toBeGreaterThan(boxes[0].x - 1); // Instance sits right of Display in the first row
				expect(Math.abs(boxes[1].y - boxes[0].y)).toBeLessThan(2);
				expect(boxes[3].x).toBeGreaterThan(boxes[2].x - 1); // Access sits right of PDF in the second row
				expect(Math.abs(boxes[3].y - boxes[2].y)).toBeLessThan(2);
			} else {
				expect(new Set(boxes.map((b) => Math.round(b.x))).size).toBe(1); // one column
			}
			// Content starts at the top of every card body, also when the card stretches to its row neighbour.
			const offsets = await page.locator("#general-settings-form .settings-card").evaluateAll((els) => els.map((el) => {
				const body = el.querySelector(".card-body");
				return el.querySelector(".card-body > *").getBoundingClientRect().top - body.getBoundingClientRect().top;
			}));
			for (const o of offsets) expect(Math.abs(o - offsets[0])).toBeLessThan(2);
			if (process.env.JUSTUP_SHOT_DIR) await page.screenshot({ path: `${process.env.JUSTUP_SHOT_DIR}/general-${name}.png`, fullPage: true });
		});
	}
});
