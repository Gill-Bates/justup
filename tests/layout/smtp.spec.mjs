//
// tests/layout/smtp.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test } from "@playwright/test";
import { renderTemplates, startStaticAppServer } from "./test-support.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
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
window.JuFieldErrors = { clear() {}, show() {} };
</script>`;
const PAGE = (smtp) => `<!doctype html><html lang="en"><head><meta charset="utf-8"></head><body>${STUB}
<div id="settings-form"><div id="smtp-host">${render(smtp)}</div></div>
<script src="/static/js/utils/status-badge.js"></script>
<script type="module">
import { initStorage, loadSettings } from "/static/js/settings.storage.js";
import { resetDomCache } from "/static/js/settings.core.js";
window.__storage = { initStorage, loadSettings, resetDomCache };
initStorage();
window.__ready = true;
</script></body></html>`;

function render(smtp) {
	return renderTemplates(ROOT, [{ template: "settings/smtp.html", context: { smtp, implicit_tls_port: 465, template: { subject: "s", html: "<p>h</p>" } } }]);
}

let current = "";
let server;
let baseUrl;
let browserErrors = [];

const respond = (page, index, success, body = {}) => page.evaluate(([i, good, payload]) => {
	window.__calls[i].resolve({ ok: good, status: good ? 200 : 500, json: async () => payload });
}, [index, success, body]);
const calls = (page) => page.evaluate(() => window.__calls.map((call) => ({ url: call.url, method: call.method, body: call.body })));
const waitForCalls = async (page, count) => expect.poll(async () => (await calls(page)).length).toBeGreaterThanOrEqual(count);
const waitForIdle = async (page) => expect.poll(() => page.evaluate(() => window.justUpSettingsSavePending === null)).toBe(true);
const tls = (page) => page.evaluate(() => {
	const el = document.getElementById("smtp_use_tls");
	return {
		checked: el.checked, disabled: el.disabled, stored: el.dataset.storedValue ?? null, wanted: el.dataset.wantedValue ?? null,
		label: document.querySelector('label[for="smtp_use_tls"]').textContent.trim(),
	};
});
const waitForTls = async (page, expected) => expect.poll(() => tls(page)).toMatchObject(expected);
const setPort = (page, value) => page.evaluate((port) => {
	const input = document.getElementById("smtp_port");
	input.value = port;
	input.dispatchEvent(new Event("input", { bubbles: true }));
}, value);
const loadPage = async (page, smtp) => {
	current = PAGE(smtp);
	await page.goto(baseUrl);
	await page.waitForFunction(() => window.__ready === true);
};
test.describe("SMTP settings browser regressions @smtp-ui", () => {
	test.beforeAll(async () => {
		current = PAGE(BASE_SMTP);
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

	test("TLS defaults on when the stored value is absent", async ({ page }) => {
		await loadPage(page, BASE_SMTP);
		await expect.poll(async () => (await tls(page)).checked).toBe(true);
		await page.evaluate(() => {
			const input = document.getElementById("smtp_use_tls");
			input.removeAttribute("data-stored-value");
			input.checked = false;
			window.__storage.initStorage();
		});
		await expect.poll(async () => (await tls(page)).checked).toBe(true);
	});

	test("stored false survives settings reload and an HTMX swap", async ({ page }) => {
		await loadPage(page, { ...BASE_SMTP, smtp_use_tls: false });
		await expect.poll(() => tls(page)).toMatchObject({ checked: false, stored: "false" });
		const reload = page.evaluate(() => window.__storage.loadSettings());
		await respond(page, 0, true, {
			smtp_enabled: true, smtp_host: "mail.example.com", smtp_port: 587, smtp_from: "alerts@example.com", smtp_use_tls: false,
		});
		await reload;
		await expect.poll(async () => (await tls(page)).checked).toBe(false);
		await page.evaluate((html) => {
			document.getElementById("smtp-host").innerHTML = html;
			document.body.dispatchEvent(new CustomEvent("htmx:afterSwap", { bubbles: true }));
			window.__storage.initStorage();
		}, render({ ...BASE_SMTP, smtp_use_tls: false }));
		await expect.poll(async () => (await tls(page)).checked).toBe(false);
	});

	test("port 465 locks TLS on and leaving it restores stored state", async ({ page }) => {
		await loadPage(page, { ...BASE_SMTP, smtp_use_tls: false });
		const initialCalls = (await calls(page)).length;
		await setPort(page, "465");
		let state = await tls(page);
		expect(state).toMatchObject({ checked: true, disabled: true, label: "Implicit TLS (SMTPS)" });
		await setPort(page, "587");
		state = await tls(page);
		expect(state).toMatchObject({ checked: false, disabled: false, label: "Use STARTTLS" });
		expect(await calls(page)).toHaveLength(initialCalls);
	});

	test("failed TLS save rolls the switch back to the stored state", async ({ page }) => {
		await loadPage(page, BASE_SMTP);
		await page.click("#smtp_use_tls");
		await waitForCalls(page, 1);
		await expect.poll(async () => (await tls(page)).checked).toBe(false);
		await respond(page, 0, false);
		await waitForTls(page, { checked: true, stored: "true", wanted: null });
	});

	test("partial settings response leaves SMTP fields unchanged", async ({ page }) => {
		await loadPage(page, { ...BASE_SMTP, smtp_user: "alerts", smtp_use_tls: false });
		const partial = page.evaluate(() => window.__storage.loadSettings());
		await respond(page, 0, true, { signal_enabled: false });
		await partial;
		const kept = await page.evaluate(() => ({
			host: document.getElementById("smtp_host").value, port: document.getElementById("smtp_port").value,
			from: document.getElementById("smtp_from").value, user: document.getElementById("smtp_user").value,
			enabled: document.getElementById("smtp_enabled").checked, tls: document.getElementById("smtp_use_tls").checked,
		}));
		expect(kept).toEqual({ host: "mail.example.com", port: "587", from: "alerts@example.com", user: "alerts", enabled: true, tls: false });
	});

	test("quick double toggle saves in order and clears pending state", async ({ page }) => {
		await loadPage(page, BASE_SMTP);
		await page.click("#smtp_use_tls");
		await page.click("#smtp_use_tls");
		await waitForCalls(page, 1);
		let sent = await calls(page);
		expect(sent).toHaveLength(1);
		await expect.poll(() => page.evaluate(() => window.justUpSettingsSavePending !== null)).toBe(true);
		await respond(page, 0, true);
		await waitForCalls(page, 2);
		sent = await calls(page);
		expect(sent.map((call) => call.body.smtp_use_tls)).toEqual([false, true]);
		await respond(page, 1, true);
		await waitForIdle(page);
		await expect.poll(() => tls(page)).toMatchObject({ checked: true, stored: "true", wanted: null });
	});

	test("failed first save does not roll back a successful second save", async ({ page }) => {
		await loadPage(page, BASE_SMTP);
		await page.click("#smtp_use_tls");
		await page.click("#smtp_use_tls");
		await waitForCalls(page, 1);
		await respond(page, 0, false);
		await waitForCalls(page, 2);
		await respond(page, 1, true);
		await waitForIdle(page);
		await expect.poll(() => tls(page)).toMatchObject({ checked: true, stored: "true" });
	});

	test("failed newest save restores the last confirmed TLS value", async ({ page }) => {
		await loadPage(page, BASE_SMTP);
		await page.click("#smtp_use_tls");
		await waitForCalls(page, 1);
		await respond(page, 0, true);
		await waitForIdle(page);
		await page.click("#smtp_use_tls");
		await waitForCalls(page, 2);
		await respond(page, 1, false);
		await waitForTls(page, { checked: false, stored: "false", wanted: null });
	});

	test("port changes during a pending save preserve the user's choice", async ({ page }) => {
		await loadPage(page, BASE_SMTP);
		await page.click("#smtp_use_tls");
		await waitForCalls(page, 1);
		await setPort(page, "465");
		await expect.poll(async () => (await tls(page)).checked).toBe(true);
		await setPort(page, "587");
		await expect.poll(() => tls(page)).toMatchObject({ checked: false, stored: "true" });
		await respond(page, 0, true);
		await waitForIdle(page);
		await waitForTls(page, { checked: false, stored: "false", wanted: null });
	});

	test("same-key host saves retain their order", async ({ page }) => {
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
		let sent = await calls(page);
		expect(sent).toHaveLength(1);
		expect(sent[0].body.smtp_host).toBe("first.example.com");
		await respond(page, 0, true);
		await waitForCalls(page, 2);
		sent = await calls(page);
		expect(sent[1].body.smtp_host).toBe("second.example.com");
		await respond(page, 1, true);
		await waitForIdle(page);
	});

	test("late preview response cannot replace the most recently selected variant", async ({ page }) => {
		await loadPage(page, BASE_SMTP);
		const previewStart = (await calls(page)).length;
		await page.addScriptTag({ url: "/static/js/settings.smtp-template.js" });
		await page.evaluate(() => document.dispatchEvent(new Event("DOMContentLoaded")));
		await waitForCalls(page, previewStart + 1);
		await page.click("#btn-preview-resolved");
		await waitForCalls(page, previewStart + 2);
		const previews = await page.evaluate(() => window.__calls.map((call, index) => ({
			index, url: call.url, body: call.body, aborted: call.signal?.aborted,
		})).filter((call) => call.url === "/settings/smtp/template/preview"));
		expect(previews[0].aborted).toBe(true);
		await respond(page, previews[1].index, true, { html: "<html><body>resolved-preview</body></html>" });
		await expect.poll(() => page.locator("#email_preview_frame").evaluate((frame) => frame.srcdoc.includes("resolved-preview"))).toBe(true);
		await respond(page, previews[0].index, true, { html: "<html><body>down-preview</body></html>" });
		// Let the page process the stale answer first; polling alone would pass before it arrived.
		await page.evaluate(() => new Promise((resolve) => setTimeout(resolve, 0)));
		await expect.poll(() => page.locator("#email_preview_frame").evaluate((frame) => frame.srcdoc)).toContain("resolved-preview");
	});

	test("editor mode and preview variant buttons keep aria-pressed in step with the active state", async ({ page }) => {
		await loadPage(page, BASE_SMTP);
		await page.addScriptTag({ url: "/static/js/settings.smtp-template.js" });
		await page.evaluate(() => document.dispatchEvent(new Event("DOMContentLoaded")));
		const state = (ids) => page.evaluate((list) => list.map((id) => {
			const button = document.getElementById(id);
			return [id, button.getAttribute("aria-pressed"), button.classList.contains("active")];
		}), ids);
		const modes = ["btn-mode-edit", "btn-mode-html", "btn-mode-preview"];
		const variants = ["btn-preview-down", "btn-preview-resolved"];
		const expectPressed = async (ids, pressedId) => {
			const rows = await state(ids);
			for (const [id, pressed, active] of rows) {
				expect(pressed, `${id} aria-pressed`).toBe(id === pressedId ? "true" : "false");
				expect(active, `${id} .active`).toBe(id === pressedId);
			}
		};
		await expectPressed(modes, "btn-mode-preview");
		await expectPressed(variants, "btn-preview-down");
		await page.click("#btn-mode-html");
		await expectPressed(modes, "btn-mode-html");
		await page.click("#btn-mode-preview");
		await expectPressed(modes, "btn-mode-preview");
		await page.click("#btn-preview-resolved");
		await expectPressed(variants, "btn-preview-resolved");
		await page.click("#btn-preview-down");
		await expectPressed(variants, "btn-preview-down");
	});
	test("the placeholders disclosure is a button that the Space key opens", async ({ page }) => {
		await loadPage(page, BASE_SMTP);
		await page.addScriptTag({ url: "/static/js/bootstrap.bundle.min.js" });
		const toggle = page.locator('[data-bs-target="#placeholdersCollapse"]');
		expect(await toggle.evaluate((el) => el.tagName)).toBe("BUTTON");
		await toggle.focus();
		await expect(toggle).toHaveAttribute("aria-expanded", "false");
		await page.keyboard.press("Space");
		await expect(toggle).toHaveAttribute("aria-expanded", "true");
		await expect(page.locator("#placeholdersCollapse")).toBeVisible();
		await page.keyboard.press("Space");
		await expect(toggle).toHaveAttribute("aria-expanded", "false");
	});
	test("failed autosave prevents the test-mail request", async ({ page }) => {
		await loadPage(page, BASE_SMTP);
		await page.addScriptTag({ url: "/static/js/settings.users.js" });
		await page.evaluate(() => document.dispatchEvent(new Event("DOMContentLoaded")));
		await page.click("#smtp_use_tls");
		await waitForCalls(page, 1);
		await page.locator("#smtp_test_recipient").fill("test@example.com");
		await page.evaluate(() => { void window.SettingsPage.Users.testSmtp(); });
		await respond(page, 0, false);
		await expect(page.locator("#test-smtp-btn")).toBeEnabled();
		const testMailCalls = (await calls(page)).filter((call) => call.url === "/settings/smtp/test-mail");
		expect(testMailCalls).toHaveLength(0);
		expect(await page.evaluate(() => window.__errors.includes("Could not save settings. Test email was not sent."))).toBe(true);
	});
});
