//
// tests/layout/modal.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Browser regression checks for the modal dismissal policy, using the shared Playwright test runner.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test as base } from "@playwright/test";
import { startStaticAppServer } from "./test-support.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const MODALS = path.join(ROOT, "app", "templates", "settings", "modals.html");

const PAGE = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/static/css/bootstrap.min.css"><link rel="stylesheet" href="/static/css/ui.css">
</head><body><button type="button" id="opener">Open</button>
${readFileSync(MODALS, "utf8")}
<script src="/static/js/utils/csrf.js"></script><script src="/static/js/utils/escape.js"></script><script src="/static/js/bootstrap.bundle.min.js"></script><script src="/static/js/utils/modal-focus.js"></script><script src="/static/js/utils/field-errors.js"></script><script src="/static/js/utils/optional-groups.js"></script><script src="/static/js/utils/confirm-dialog.js"></script><script src="/static/js/app.js"></script><script src="/static/js/settings.recipients.js"></script></body></html>`;

const test = base.extend({
	modalBase: [async ({}, use) => {
		const server = await startStaticAppServer({ root: ROOT, renderPage: () => PAGE });
		try {
			await use(server.baseUrl);
		} finally {
			await server.close();
		}
	}, { scope: "worker" }],
});
test.use({ viewport: { width: 390, height: 780 }, hasTouch: true });

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
const shownConfirm = (page) => page.waitForFunction(() => document.activeElement?.id === "ju-confirm-cancel" && !document.querySelector(".modal-backdrop.fade:not(.show)"));
const closed = (page, id) => page.waitForFunction(
	(i) => !document.getElementById(i).classList.contains("show") && getComputedStyle(document.getElementById(i)).display === "none",
	id, { timeout: 3000 }).then(() => true, () => false);

test("modal dismissal, focus and scroll-lock contracts @modal", async ({ page, browserName, modalBase }) => {
	const name = browserName;
	const findings = [];
	const errors = [];
	page.on("pageerror", (e) => errors.push(String(e)));

	await page.goto(modalBase);
	const ids = await page.$$eval(".modal", (els) => els.map((el) => el.id));
	if (!ids.length) findings.push(`${name}: no modals found`);
	for (const id of ids) {
		const fail = (msg) => findings.push(`${name} #${id}: ${msg}`);
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
	if (errors.length) findings.push(`${name}: page errors: ${errors.join(" | ")}`);
	assert.deepEqual(findings, [], findings.join("\n"));
});

test("modal semantics, focus and geometry @modal", async ({ page, browserName, modalBase }) => {
	const name = browserName;
	const findings = [];
	let checked = 0;
	const errors = [];
	page.on("pageerror", (e) => errors.push(String(e)));
	await page.goto(modalBase);
	const ids = await page.$$eval(".modal", (els) => els.map((el) => el.id));
	if (!ids.length) findings.push(`${name}: no modals found`);
	// Contract checks (DESIGN.md "Modal dialogs"): semantics, focus, geometry; 320 px and desktop, light and dark.
	for (const [vw, vh] of [[320, 640], [1280, 800]]) {
		for (const theme of ["light", "dark"]) {
			await page.setViewportSize({ width: vw, height: vh });
			await page.evaluate((t) => document.documentElement.setAttribute("data-bs-theme", t), theme);
			for (const id of ids) {
				const fail = (msg) => findings.push(`${name} #${id} ${vw}px ${theme}: ${msg}`);
				checked++;
				await open(page, id);
				const r = await page.evaluate((i) => {
					const m = document.getElementById(i), out = [];
					const box = (el) => el.getBoundingClientRect();
					if (m.getAttribute("role") !== "dialog" || m.getAttribute("aria-modal") !== "true") out.push("role/aria-modal missing");
					const title = document.getElementById(m.getAttribute("aria-labelledby") || "");
					if (!title || !title.textContent.trim()) out.push("aria-labelledby does not resolve to a title");
					for (const d of (m.getAttribute("aria-describedby") || "").split(" ").filter(Boolean)) if (!document.getElementById(d)) out.push(`aria-describedby ${d} missing`);
					if (!m.contains(document.activeElement) || document.activeElement === m) out.push("focus not moved to a control inside the dialog");
					const content = m.querySelector(".modal-content"), c = box(content), de = document.documentElement;
					if (c.left < -1 || c.right > de.clientWidth + 1 || c.top < -1 || c.bottom > innerHeight + 1) out.push("dialog exceeds the viewport");
					if (de.scrollWidth > de.clientWidth + 1 || content.scrollWidth > content.clientWidth + 1) out.push("horizontal overflow");
					for (const sel of [".btn-close", ".modal-footer .btn"]) for (const el of m.querySelectorAll(sel)) {
						const b = box(el);
						if (b.width < 24 || b.height < 24) out.push(`target below 24px: ${sel}`);
						if (b.bottom > innerHeight + 1 || b.top < -1) out.push(`${sel} outside the viewport`);
					}
					const ctrls = [...m.querySelectorAll("input:not([type=hidden]):not(.visually-hidden), select, textarea, button, label")].filter((e) => e.offsetParent && e.closest(".modal-body, .modal-footer"));
					for (let a = 0; a < ctrls.length; a++) for (let b = a + 1; b < ctrls.length; b++) {
						if (ctrls[a].closest(".modal-body, .modal-footer") !== ctrls[b].closest(".modal-body, .modal-footer") || ctrls[a].contains(ctrls[b]) || ctrls[b].contains(ctrls[a]) || ctrls[a].matches("label") && ctrls[a].htmlFor === ctrls[b].id || ctrls[b].matches("label") && ctrls[b].htmlFor === ctrls[a].id) continue;
						const x = box(ctrls[a]), y = box(ctrls[b]);
						if (x.left < y.right - 1 && y.left < x.right - 1 && x.top < y.bottom - 1 && y.top < x.bottom - 1) out.push(`overlap ${ctrls[a].id || ctrls[a].tagName} / ${ctrls[b].id || ctrls[b].tagName}`);
					}
					for (const btn of m.querySelectorAll("button")) if (!(btn.getAttribute("aria-label") || btn.textContent).trim()) out.push("button without name");
					// Focus lifecycle: no autofocus attribute; critical dialogs start on a required field or the safe Cancel action.
					if (m.querySelector("[autofocus]")) out.push("autofocus attribute used instead of the focus module");
					const active = document.activeElement, body = m.querySelector(".modal-body");
					const firstField = [...body.querySelectorAll("input:not([type=hidden]):not([type=checkbox]):not(.visually-hidden), select, textarea")].find((e) => e.offsetParent && !e.disabled && !e.readOnly);
					const critical = m.classList.contains("ju-modal--critical");
					if (critical) {
						const req = [...body.querySelectorAll("input[required], select[required], textarea[required]")].find((e) => e.offsetParent);
						const cancel = m.querySelector(".modal-footer [data-bs-dismiss='modal']");
						if (req ? active !== req : active !== cancel) out.push("critical dialog: initial focus is neither the required field nor Cancel");
					} else if (firstField && active !== firstField) out.push("form dialog: initial focus is not the first field");
					// Actions: Cancel/Close in the footer, plus one submit-type primary action unless the dialog is Close-only.
					const dismiss = m.querySelectorAll(".modal-footer [data-bs-dismiss='modal']");
					if (!dismiss.length) out.push("footer has no Cancel/Close action");
					const footerBtns = m.querySelectorAll(".modal-footer .btn");
					if (footerBtns.length > 1 && ![...footerBtns].some((b) => /\bbtn-(primary|danger|warning)\b/.test(b.className))) out.push("footer has no primary action");
					// Long dialogs: the body scrolls (not the page) and the footer stays inside the viewport.
					const foot = m.querySelector(".modal-footer");
					if (foot) { const f = foot.getBoundingClientRect(); if (f.bottom > innerHeight + 1) out.push("footer not reachable without scrolling the page"); }
					if (body.scrollHeight > body.clientHeight + 1 && getComputedStyle(body).overflowY === "visible") out.push("body overflows without scrolling");
					// Field errors: aria-invalid, resolving aria-describedby, error next to the field, cleared on edit.
					if (firstField && firstField.id && window.JuFieldErrors) {
						window.JuFieldErrors.show(m, [{ id: firstField.id, message: "Test error" }]);
						const err = document.getElementById(`${firstField.id}-field-error`);
						if (firstField.getAttribute("aria-invalid") !== "true") out.push("field error: aria-invalid missing");
						if (!err || !(firstField.getAttribute("aria-describedby") || "").split(" ").includes(err.id)) out.push("field error: aria-describedby does not resolve");
						else if (err.previousElementSibling !== (firstField.closest(".ju-switch") || firstField)) out.push("field error is not directly below its field");
						if (document.activeElement !== firstField) out.push("field error: focus not on the invalid field");
						firstField.dispatchEvent(new Event("input", { bubbles: true }));
						if (firstField.hasAttribute("aria-invalid") || document.getElementById(`${firstField.id}-field-error`)) out.push("field error: not cleared after editing");
					}
					return out;
				}, id);
				for (const msg of r) fail(msg);
				// Focus trap: Tab never lands on content behind the dialog (a moment on <body> is the browser edge).
				for (let n = 0; n < 12; n++) {
					await page.keyboard.press("Tab");
					if (!(await page.evaluate((i) => { const a = document.activeElement; return a === document.body || document.getElementById(i).contains(a); }, id))) { fail("Tab reached content behind the dialog"); break; }
				}
				await page.evaluate((i) => document.getElementById(i).querySelector("input:not(.visually-hidden), .modal-footer .btn")?.focus(), id);
				await page.keyboard.press("Escape");
				if (!(await closed(page, id))) {
					fail("Escape did not close the dialog");
					await page.evaluate((i) => window.bootstrap.Modal.getInstance(document.getElementById(i)).hide(), id);
					await closed(page, id);
				}
			}
		}
	}
	await page.setViewportSize({ width: 390, height: 780 });
	if (errors.length) findings.push(`${name}: page errors: ${errors.join(" | ")}`);
	assert.ok(checked > 0, "expected modal geometry checks");
	assert.deepEqual(findings, [], findings.join("\n"));
});

test("confirmAction keyboard behavior and concurrency @modal", async ({ page, browserName, modalBase }) => {
	const name = browserName;
	const findings = [];
	let checked = 0;
	const errors = [];
	page.on("pageerror", (e) => errors.push(String(e)));
	await page.goto(modalBase);
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
	// Concurrent confirmAction calls: the second is rejected with false and leaves the active dialog fully usable.
	{
		const fail = (msg) => findings.push(`${name} confirmAction concurrency: ${msg}`);
		checked++;
		await page.focus("#opener");
		await page.evaluate(() => {
			window.__confirmResults = [];
			window.confirmAction({ title: "T", message: "M", confirmLabel: "Do it" }).then((ok) => window.__confirmResults.push(["first", ok]));
			window.confirmAction({ title: "T2", message: "M2", confirmLabel: "Other" }).then((ok) => window.__confirmResults.push(["second", ok]));
		});
		await shownConfirm(page);
		const second = await page.evaluate(() => JSON.stringify(window.__confirmResults));
		if (second !== '[["second",false]]') fail(`second call not rejected alone: ${second}`);
		if ((await page.textContent("#ju-confirm-ok")) !== "Do it") fail("active dialog was altered by the second call");
		await page.keyboard.press("Tab");
		await page.keyboard.press("Enter");
		const done = await page.waitForFunction(() => window.__confirmResults.length === 2, null, { timeout: 3000 }).then(() => true, () => false);
		if (!done || (await page.evaluate(() => JSON.stringify(window.__confirmResults))) !== '[["second",false],["first",true]]') fail("Confirm of the first dialog no longer works");
		await closed(page, "ju-confirm-modal");
	}
	if (errors.length) findings.push(`${name}: page errors: ${errors.join(" | ")}`);
	assert.ok(checked > 0, "expected confirmAction checks");
	assert.deepEqual(findings, [], findings.join("\n"));
	});

	test("recipient confirmation handoff and recovery @modal", async ({ page, browserName, modalBase }) => {
	const name = browserName;
	const findings = [];
	let checked = 0;
	const errors = [];
	page.on("pageerror", (e) => errors.push(String(e)));
	await page.goto(modalBase);
	// The real saveRecipient() (settings.recipients.js, network stubbed): switching "Enable for all Targets" off asks for
	// confirmation from inside the open Edit dialog. Keyboard only; the dialog is handed over, never stacked.
	for (const answer of ["cancel", "confirm"]) {
		const fail = (msg) => findings.push(`${name} saveRecipient confirmation (${answer}): ${msg}`);
		checked++;
		await page.evaluate(() => {
			window.__patches = [];
			window.__saves = [];
			const respond = (body) => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
			window.apiFetch = (url, options) => {
				if (options && options.method === "PATCH") { window.__patches.push(JSON.parse(options.body)); return respond({ warnings: [] }); }
				if (url === "/recipients/1") return respond({ id: 1, name: "Original", is_enabled: true, enable_for_all_targets: true, phone: "", email: "a@example.com", targets: [{ id: 1 }, { id: 2 }] });
				return respond([]);
			};
		});
		await page.focus("#opener");
		await page.evaluate(() => window.SettingsPage.Recipients.editRecipient(1));
		await page.waitForFunction(() => document.getElementById("editRecipientModal").classList.contains("show") && document.getElementById("editRecipientModal").contains(document.activeElement) && !document.querySelector(".modal-backdrop.fade:not(.show)"));
		await page.fill("#edit-recipient-name", "Draft name");
		await page.focus("#edit-recipient-all-targets");
		await page.keyboard.press("Space"); // switch off, as in the review scenario
		await page.keyboard.press("Tab");
		await page.keyboard.press("Tab");
		if ((await page.evaluate(() => document.activeElement?.id)) !== "edit-recipient-btn") fail("Tab does not reach Save Changes");
		const draft = () => page.evaluate(() => [document.getElementById("edit-recipient-name").value, document.getElementById("edit-recipient-all-targets").checked]);
		const before = await draft();
		await page.keyboard.press("Enter");
		await shownConfirm(page);
		const stacked = await page.evaluate(() => ({
			shown: [...document.querySelectorAll(".modal.show")].map((m) => m.id),
			backdrops: document.querySelectorAll(".modal-backdrop").length,
		}));
		if (stacked.shown.join() !== "ju-confirm-modal" || stacked.backdrops !== 1) fail(`dialogs stacked: shown=${stacked.shown} backdrops=${stacked.backdrops}`);
		if (answer === "cancel") await page.keyboard.press("Escape");
		else { await page.keyboard.press("Tab"); await page.keyboard.press("Enter"); }
		if (answer === "cancel") {
			await page.waitForFunction(() => document.getElementById("editRecipientModal").classList.contains("show") && document.activeElement?.id === "edit-recipient-btn", null, { timeout: 3000 }).catch(() => {});
			const back = await page.evaluate(() => {
				const m = document.getElementById("editRecipientModal"), save = document.getElementById("edit-recipient-btn");
				return {
					shown: m.classList.contains("show"), ariaModal: m.getAttribute("aria-modal"), ariaHidden: m.getAttribute("aria-hidden"),
					scrollLock: document.body.classList.contains("modal-open") && document.body.style.overflow === "hidden",
					backdrops: document.querySelectorAll(".modal-backdrop").length, active: document.activeElement?.id, saveDisabled: save.disabled,
					patches: window.__patches.length,
				};
			});
			if (!back.shown || back.ariaModal !== "true" || back.ariaHidden !== null) fail(`edit dialog not restored: ${JSON.stringify(back)}`);
			if (!back.scrollLock || back.backdrops !== 1) fail(`scroll lock/backdrop inconsistent: ${JSON.stringify(back)}`);
			if (back.saveDisabled || back.active !== "edit-recipient-btn") fail(`Save Changes not enabled and focused: ${JSON.stringify(back)}`);
			if (back.patches) fail("a save request was sent although the confirmation was cancelled");
			if (JSON.stringify(await draft()) !== JSON.stringify(before)) fail("draft values changed");
			for (let n = 0; n < 8; n++) {
				await page.keyboard.press("Tab");
				if (!(await page.evaluate(() => { const a = document.activeElement; return a === document.body || document.getElementById("editRecipientModal").contains(a); }))) { fail("Tab left the restored dialog"); break; }
			}
			await page.focus("#edit-recipient-btn"); // Tab may rest on <body> at the wrap point; Escape needs focus in the dialog
			await page.keyboard.press("Escape");
		} else {
			await page.waitForFunction(() => window.__patches.length > 0, null, { timeout: 3000 }).catch(() => fail("Confirm did not save"));
			const sent = await page.evaluate(() => window.__patches);
			if (sent.length !== 1 || sent[0].enable_for_all_targets !== false || sent[0].name !== "Draft name") fail(`unexpected save payload: ${JSON.stringify(sent)}`);
		}
		if (!(await closed(page, "editRecipientModal"))) {
			fail("edit dialog did not close");
			await page.evaluate(() => window.bootstrap.Modal.getInstance(document.getElementById("editRecipientModal")).hide());
			await closed(page, "editRecipientModal");
		}
		await page.waitForFunction(() => !document.body.classList.contains("modal-open") && !document.querySelector(".modal-backdrop") && document.activeElement?.id === "opener", null, { timeout: 2000 }).catch(() => {});
		const end = await page.evaluate(() => ({
			active: document.activeElement?.id, open: document.body.classList.contains("modal-open"), overflow: document.body.style.overflow,
			backdrops: document.querySelectorAll(".modal-backdrop").length, saveDisabled: document.getElementById("edit-recipient-btn").disabled,
		}));
		if (end.active !== "opener") fail(`focus not returned to the opener: ${end.active}`);
		if (end.open || end.overflow !== "" || end.backdrops) fail(`scroll lock/backdrop left behind: ${JSON.stringify(end)}`);
		if (end.saveDisabled) fail("Save Changes left disabled");
	}
	if (errors.length) findings.push(`${name}: page errors: ${errors.join(" | ")}`);
	assert.ok(checked > 0, "expected modal browser checks");
	assert.deepEqual(findings, [], findings.join("\n"));
});
