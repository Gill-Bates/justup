//
// tests/layout/recipient-channels.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// DESIGN.md "Optional field groups": a switched-off notification channel hides its whole field group
// (not just disables it) in the Edit Recipient dialog; the switch keeps focus; validation skips it.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test as base } from "@playwright/test";
import { startStaticAppServer } from "./test-support.mjs";
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const MODALS = path.join(ROOT, "app", "templates", "settings", "modals.html");
const SCRIPTS = ["utils/csrf.js", "utils/escape.js", "bootstrap.bundle.min.js", "utils/modal-focus.js", "utils/field-errors.js", "utils/optional-groups.js", "utils/confirm-dialog.js", "app.js", "settings.recipients.js"];
const PAGE = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/static/css/bootstrap.min.css"><link rel="stylesheet" href="/static/css/material-icons.css"><link rel="stylesheet" href="/static/css/gentelella-layout.css"><link rel="stylesheet" href="/static/css/ui.css">
</head><body><button type="button" id="opener">Open</button>
${readFileSync(MODALS, "utf8").replace(/\{#[\s\S]*?#\}/g, "")}
${SCRIPTS.map((src) => `<script src="/static/js/${src}"></script>`).join("")}</body></html>`;
const VIEWPORTS = [
	{ name: "1280x720", width: 1280, height: 720 },
	{ name: "390x844", width: 390, height: 844 },
	{ name: "320x640", width: 320, height: 640 },
	{ name: "200pct-zoom", width: 640, height: 360 }, // 1280x720 at 200 % browser zoom
];
const SHOTS = process.env.JUSTUP_CHANNEL_SHOTS;
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
const ID = { signal: "edit-recipient-signal-enabled", email: "edit-recipient-email-enabled" };
// Everything a channel owns; none of it may be reachable or visible while its switch is off.
const GROUP = { signal: ["edit-recipient-phone", "edit-recipient-phone-help", "edit-recipient-test-signal"], email: ["edit-recipient-email", "edit-recipient-email-help", "edit-recipient-test-email"] };
const state = (page) => page.evaluate((groups) => {
	const visible = (el) => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== "hidden";
	const out = {};
	for (const [channel, ids] of Object.entries(groups)) out[channel] = ids.map((id) => visible(document.getElementById(id)));
	out.hint = visible(document.getElementById("edit-recipient-test-hint"));
	out.active = document.activeElement?.id;
	return out;
}, GROUP);
const allVisible = (s, channel, expected) => assert.deepEqual(s[channel], [expected, expected, expected], `${channel} group visible=${expected}: ${JSON.stringify(s)}`);
// Tab through the whole dialog twice and report every id that received focus.
const tabOrder = async (page) => {
	const seen = new Set();
	await page.focus("#edit-recipient-name");
	for (let n = 0; n < 16; n += 1) {
		await page.keyboard.press("Tab");
		seen.add(await page.evaluate(() => document.activeElement?.id || document.activeElement?.tagName));
	}
	return seen;
};
const geometry = (page) => page.evaluate(() => {
	const de = document.documentElement, modal = document.getElementById("editRecipientModal");
	const content = modal.querySelector(".modal-content").getBoundingClientRect();
	const footer = modal.querySelector(".modal-footer").getBoundingClientRect();
	const body = modal.querySelector(".modal-body");
	const problems = [];
	if (de.scrollWidth > de.clientWidth + 1) problems.push("page overflows horizontally");
	if (body.scrollWidth > body.clientWidth + 1) problems.push("dialog body overflows horizontally");
	if (content.left < -1 || content.right > de.clientWidth + 1 || content.top < -1 || content.bottom > innerHeight + 1) problems.push(`dialog outside the viewport ${JSON.stringify(content)}`);
	if (footer.bottom > innerHeight + 1 || footer.top < -1) problems.push("footer not visible");
	for (const btn of modal.querySelectorAll(".modal-footer .btn")) if (btn.getBoundingClientRect().bottom > innerHeight + 1) problems.push("footer button outside the viewport");
	// A channel row is separated by exactly one rule: no two adjacent visible borders.
	const rows = [...modal.querySelectorAll(".ju-channel")];
	const gaps = rows.map((row) => getComputedStyle(row).borderTopWidth);
	if (new Set(gaps).size !== 1) problems.push(`inconsistent channel separators ${gaps}`);
	return { problems, width: Math.round(content.width) };
});
const shoot = async (page, name, theme, viewport) => {
	if (SHOTS) await page.locator("#editRecipientModal .modal-content").screenshot({ path: path.join(SHOTS, `${name}-${theme}-${viewport}.png`) });
};
for (const viewport of VIEWPORTS) {
	for (const theme of ["light", "dark"]) {
		test(`Edit Recipient hides the fields of a switched-off channel at ${viewport.name} ${theme} @modal @recipient-channels`, async ({ page, base }) => {
			const errors = [];
			page.on("pageerror", (e) => errors.push(String(e)));
			page.on("console", (m) => { if (m.type() === "error" && !m.text().startsWith("Failed to load resource")) errors.push(m.text()); }); // the static harness has no favicon
			await page.setViewportSize({ width: viewport.width, height: viewport.height });
			await page.goto(base);
			await page.evaluate((t) => {
				document.documentElement.setAttribute("data-bs-theme", t);
				window.__patches = [];
				const respond = (body) => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
				window.apiFetch = (url, options) => {
					if (options && options.method === "PATCH") { window.__patches.push(JSON.parse(options.body)); return respond({ warnings: [] }); }
					if (url === "/recipients/1") return respond({ id: 1, name: "Martin", is_enabled: true, enable_for_all_targets: false, phone: "", email: "martin@example.com", targets: [] });
					return respond([]);
				};
			}, theme);
			await page.focus("#opener");
			await page.evaluate(() => window.SettingsPage.Recipients.editRecipient(1));
			await page.waitForFunction(() => document.getElementById("editRecipientModal").classList.contains("show") && !document.querySelector(".modal-backdrop.fade:not(.show)"));
			const check = async (label) => {
				const g = await geometry(page);
				assert.deepEqual(g.problems, [], `${label}: ${g.problems.join("; ")}`);
				return g.width;
			};

			// Signal off, Email on (the reported screenshot): only the Signal header remains.
			let s = await state(page);
			allVisible(s, "signal", false);
			allVisible(s, "email", true);
			assert.equal(s.hint, true, "saved-values hint stays while a Send test is visible");
			const order = await tabOrder(page);
			for (const id of GROUP.signal) assert.ok(!order.has(id), `${id} reachable by Tab`);
			assert.ok(order.has(ID.signal) && order.has("edit-recipient-email") && order.has("edit-recipient-test-email"), `tab order ${[...order]}`);
			assert.equal(await page.getAttribute(`#${ID.signal}`, "aria-controls"), "edit-recipient-phone-container");
			assert.equal(await page.getAttribute("#edit-recipient-phone-container", "hidden") !== null, true);
			const width = await check("signal off");
			await shoot(page, "signal-off", theme, viewport.name);

			// Signal on: the group appears, focus stays on the switch, the first field is not focused.
			await page.focus(`#${ID.signal}`);
			await page.keyboard.press("Space");
			s = await state(page);
			allVisible(s, "signal", true);
			assert.equal(s.active, ID.signal, "focus left the switch when the group appeared");
			assert.equal(await check("both on"), width, "dialog width jumped");
			await page.fill("#edit-recipient-phone", "+4915112345678");
			await shoot(page, "both-on", theme, viewport.name);

			// Signal off and on again within the open dialog keeps the entered value; focus stays on the switch.
			await page.focus(`#${ID.signal}`);
			await page.keyboard.press("Space");
			s = await state(page);
			allVisible(s, "signal", false);
			assert.equal(s.active, ID.signal);
			await page.keyboard.press("Space");
			assert.equal(await page.inputValue("#edit-recipient-phone"), "+4915112345678", "value lost on off/on");
			await page.keyboard.press("Space");

			// Both off: at least one channel is required, shown as a field error; hint and groups are hidden.
			await page.focus(`#${ID.email}`);
			await page.keyboard.press("Space");
			s = await state(page);
			allVisible(s, "signal", false);
			allVisible(s, "email", false);
			assert.equal(s.hint, false, "saved-values hint shown without a Send test");
			await page.click("#edit-recipient-btn");
			await page.waitForSelector("#edit-recipient-signal-enabled-field-error");
			assert.match(await page.textContent("#edit-recipient-signal-enabled-field-error"), /at least one notification channel/i);
			assert.equal(await page.evaluate(() => window.__patches.length), 0, "saved without any channel");
			await check("both off");
			await shoot(page, "both-off", theme, viewport.name);

			// Switching a channel on clears the error; saving with only Email sends phone null.
			await page.focus(`#${ID.email}`);
			await page.keyboard.press("Space");
			assert.equal(await page.locator(".ju-field-error:visible").count(), 0, "channel error not cleared");
			s = await state(page);
			allVisible(s, "email", true);
			await page.focus("#edit-recipient-btn");
			await page.keyboard.press("Enter");
			await page.waitForFunction(() => window.__patches.length >= 1);
			const sent = await page.evaluate(() => window.__patches[0]);
			assert.equal(sent.phone, null);
			assert.equal(sent.email, "martin@example.com");
			assert.deepEqual(errors, [], errors.join("\n"));
		});
	}
}
