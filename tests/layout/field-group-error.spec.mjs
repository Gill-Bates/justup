//
// tests/layout/field-group-error.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test } from "@playwright/test";
import { renderTemplates, startStaticAppServer } from "./test-support.mjs";
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const STUB = `<script>
// A real request resolves in a later task, not inside the input event that triggered it.
window.apiFetch = async (url) => {
    await new Promise((resolve) => setTimeout(resolve, 50));
    if (String(url).includes("verify")) return { ok: false, status: 400, json: async () => ({ detail: "Invalid code" }), text: async () => "Invalid code" };
    return { ok: true, status: 200, json: async () => ({}), text: async () => "{}" };
};
window.justUpToast = { success() {}, error() {}, warning() {}, info() {} };
</script>`;
const body = () => renderTemplates(ROOT, [{ template: "settings/signal.html", context: { signal_enabled: true, signal_template: { down: "d", up: "u" } } }]);
const PAGE = () => `<!doctype html><html lang="en"><head><meta charset="utf-8">
<link rel="stylesheet" href="/static/css/ui.css"></head><body>${STUB}
<div id="settings-form">${body()}</div>
<script src="/static/js/utils/field-errors.js"></script>
<script type="module">
import { signalRegister } from "/static/js/settings.signal-registration.js";
window.__register = signalRegister;
window.__ready = true;
</script></body></html>`;
let server;
let errors = [];
const group = "#signal-verify-boxes";
const sizes = [{ width: 320, height: 640 }, { width: 390, height: 844 }];
test.describe("Group field error on the Signal code boxes @field-group-error", () => {
	test.beforeAll(async () => {
		server = await startStaticAppServer({ root: ROOT, renderPage: PAGE });
	});
	test.afterAll(async () => {
		if (server) await server.close();
	});
	test.beforeEach(({ page }) => {
		errors = [];
		page.on("pageerror", (error) => errors.push(String(error)));
	});
	test.afterEach(() => expect(errors).toEqual([]));
	for (const size of sizes) {
		for (const theme of ["light", "dark"]) {
			test(`failed commit shows a persistent group error, ${size.width}px ${theme}`, async ({ page }) => {
				await page.setViewportSize(size);
				await page.goto(server.baseUrl);
				await page.waitForFunction(() => window.__ready === true);
				await page.evaluate((mode) => document.documentElement.setAttribute("data-bs-theme", mode), theme);
				const fieldset = page.locator(group);
				expect(await fieldset.evaluate((el) => el.tagName)).toBe("FIELDSET");
				expect(await fieldset.locator("> legend").count()).toBe(1);
				await page.evaluate(async () => {
					document.getElementById("signal-reg-step-2").classList.remove("signal-step-disabled");
					document.getElementById("signal-register-number").value = "+491701234567";
					await window.__register();
				});
				const boxes = page.locator(".signal-code-box");
				await expect(boxes.first()).toBeEnabled();
				for (let i = 0; i < 6; i += 1) await page.locator(`#signal-code-box-${i}`).fill(String(i + 1));
				const error = page.locator("#signal-verify-boxes-field-error");
				await expect(error).toBeVisible();
				await expect(error).toContainText("not accepted");
				expect(await error.getAttribute("role")).toBeNull();
				await expect(fieldset).toHaveAttribute("aria-describedby", /signal-verify-boxes-field-error/);
				for (let i = 0; i < 6; i += 1) {
					await expect(page.locator(`#signal-code-box-${i}`)).toHaveAttribute("aria-describedby", /signal-verify-boxes-field-error/);
					await expect(page.locator(`#signal-code-box-${i}`)).toHaveAttribute("aria-invalid", "true");
				}
				await expect(page.locator("#signal-code-box-0")).toBeFocused();
				// Deliberate 700 ms window: the former marker vanished after 600 ms.
				const t0 = await page.evaluate(() => performance.now());
				await page.waitForFunction((start) => performance.now() > start + 700, t0);
				await expect(error).toBeVisible();
				const box = await error.boundingBox();
				expect(box.x + box.width).toBeLessThanOrEqual(size.width + 1);
				await page.locator("#signal-code-box-0").fill("7");
				await expect(error).toHaveCount(0);
				await expect(fieldset).not.toHaveAttribute("aria-describedby", /field-error/);
				await expect(page.locator("#signal-code-box-1")).not.toHaveAttribute("aria-invalid", "true");
			});
		}
	}
});
