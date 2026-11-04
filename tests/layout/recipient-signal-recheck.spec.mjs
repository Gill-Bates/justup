//
// tests/layout/recipient-signal-recheck.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Settings > Recipients "Re-check Signal" row action: keyboard operation, busy and failure states,
// result as status at the recipient, focus kept on the button, no overflow on narrow widths.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test as base } from "@playwright/test";
import { startStaticAppServer } from "./test-support.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const TEMPLATES = path.join(ROOT, "app", "templates", "settings");
const SCRIPTS = ["utils/csrf.js", "utils/escape.js", "bootstrap.bundle.min.js", "utils/modal-focus.js", "utils/field-errors.js", "utils/optional-groups.js", "utils/confirm-dialog.js", "utils/list-filter.js", "app.js", "settings.recipients.js"];
const strip = (file) => readFileSync(path.join(TEMPLATES, file), "utf8").replace(/\{#[\s\S]*?#\}/g, "");
const PAGE = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/static/css/bootstrap.min.css"><link rel="stylesheet" href="/static/css/material-icons.css"><link rel="stylesheet" href="/static/css/gentelella-layout.css"><link rel="stylesheet" href="/static/css/ui.css">
</head><body><main class="ui-page" id="host">${strip("recipients.html")}</main>${strip("modals.html")}
${SCRIPTS.map((src) => `<script src="/static/js/${src}"></script>`).join("")}</body></html>`;
const VIEWPORTS = [
	{ name: "1280x720", width: 1280, height: 720 },
	{ name: "390x844", width: 390, height: 844 },
	{ name: "320x640", width: 320, height: 640 },
];
const PEOPLE = [
	{ id: 1, name: "Martin", email: "martin@example.com", phone: "", is_enabled: true },
	{ id: 2, name: "Tobias Wolfgang Maximilian von Hohenzollern-Sigmaringen", email: "", phone: "+4915112345678", is_enabled: true },
];
const test = base.extend({
	base: [async ({}, use) => {
		const server = await startStaticAppServer({ root: ROOT, renderPage: () => PAGE });
		try {
			await use(server.baseUrl);
		} finally {
			await server.close();
		}
	}, { scope: "worker" }],
});
const BTN = '#recipients-list tr[data-recipient-id="2"] .recheck-signal-btn';
const statusLabel = (page) => page.evaluate(() => document.querySelector("#signal-status-2 .signal-status-icon").getAttribute("aria-label"));

for (const viewport of VIEWPORTS) {
	for (const theme of ["light", "dark"]) {
		test(`Re-check Signal runs from the keyboard, shows busy, result and failure at ${viewport.name} ${theme} @recipient-signal-recheck`, async ({ page, base }) => {
			const errors = [];
			page.on("pageerror", (e) => errors.push(String(e)));
			page.on("console", (m) => { if (m.type() === "error" && !m.text().startsWith("Failed to load resource")) errors.push(m.text()); });
			await page.setViewportSize({ width: viewport.width, height: viewport.height });
			await page.goto(base);
			await page.evaluate(async ({ t, people }) => {
				document.documentElement.setAttribute("data-bs-theme", t);
				window.__calls = [];
				window.__recheck = { status: 200, body: { registered: true }, release: null };
				const reply = (status, body) => ({ ok: status < 400, status, json: () => Promise.resolve(body) });
				window.apiFetch = async (url, options) => {
					window.__calls.push({ method: (options && options.method) || "GET", url });
					if (url === "/recipients") return reply(200, people);
					if (url === "/recipients/verify-signal") return reply(200, { results: { "+4915112345678": null }, invalid: [] });
					if (window.__recheck.release) await window.__recheck.release;
					return reply(window.__recheck.status, window.__recheck.body);
				};
				await window.SettingsPage.Recipients.loadRecipients();
			}, { t: theme, people: PEOPLE });
			await page.waitForSelector("#recipients-list tbody tr");

			// Only a row with a number offers the action; the accessible name names the recipient.
			assert.equal(await page.locator("#recipients-list .recheck-signal-btn").count(), 1);
			assert.match(await page.getAttribute(BTN, "aria-label"), /^Re-check Signal for Tobias/);

			// Keyboard: Enter on the focused button runs the check, busy keeps the focus, the result lands on the row.
			await page.evaluate(() => { window.__recheck.release = new Promise((resolve) => { window.__releaseRecheck = resolve; }); });
			await page.focus(BTN);
			await page.keyboard.press("Enter");
			await page.waitForSelector(`${BTN}[aria-busy="true"]`);
			assert.equal(await page.getAttribute(BTN, "aria-disabled"), "true");
			assert.equal(await page.evaluate(() => document.activeElement.classList.contains("recheck-signal-btn")), true, "focus stays on the busy button");
			assert.equal(await statusLabel(page), "Checking...");
			await page.keyboard.press("Enter"); // a second activation while busy is ignored
			await page.evaluate(() => window.__releaseRecheck());
			await page.waitForSelector(`${BTN}:not([aria-busy])`);
			assert.equal(await statusLabel(page), "Registered with Signal");
			assert.equal(await page.evaluate(() => document.activeElement.classList.contains("recheck-signal-btn")), true, "focus kept after the result");
			const posts = await page.evaluate(() => window.__calls.filter((c) => c.method === "POST" && c.url.includes("recheck")));
			assert.deepEqual(posts, [{ method: "POST", url: "/recipients/2/recheck-signal" }], "one request, id only, no number in the URL");

			// Failure: compact state on the row, button usable again.
			await page.evaluate(() => { window.__recheck = { status: 429, body: { detail: "Rate limit exceeded" }, release: null }; });
			await page.click(BTN);
			await page.waitForFunction(() => document.querySelector("#signal-status-2 .signal-status-icon").getAttribute("aria-label") === "Check failed");
			assert.equal(await page.getAttribute(BTN, "aria-disabled"), null);

			const geometry = await page.evaluate(() => {
				const de = document.documentElement;
				const btn = document.querySelector(".recheck-signal-btn").getBoundingClientRect();
				const cell = document.querySelector('tr[data-recipient-id="2"] .actions-cell').getBoundingClientRect();
				return { overflow: de.scrollWidth > de.clientWidth + 1, inside: btn.left >= cell.left - 1 && btn.right <= de.clientWidth + 1, size: Math.min(btn.width, btn.height) };
			});
			assert.equal(geometry.overflow, false, "page overflows horizontally");
			assert.equal(geometry.inside, true, "action leaves the viewport");
			assert.ok(geometry.size >= 24, "target smaller than 24 px");
			assert.deepEqual(errors, []);
		});
	}
}
