//
// tests/layout/modal.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Browser regression checks for the modal dismissal policy, using the shared Playwright test runner.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test as base } from "@playwright/test";
import { test as appTest } from "./fixtures.mjs";
import { applyTextEnlargement, recipientScripts, startStaticAppServer } from "./test-support.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const MODALS = path.join(ROOT, "app", "templates", "settings", "modals.html");

const PAGE = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/static/css/bootstrap.min.css"><link rel="stylesheet" href="/static/css/ui.css">
</head><body><button type="button" id="opener">Open</button>
${readFileSync(MODALS, "utf8")}
${recipientScripts().map((src) => `<script src="/static/js/${src}"></script>`).join("")}</body></html>`;

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
		if (!(await gone()) || !(await result(true))) fail("double click on Confirm did not resolve true");
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

// DC-SCROLL-6: Bootstrap locks <body>, which is not the scroller in the app shell, so the observable lock on <main> is
// asserted with real gestures. A positive control proves the gesture scrolls <main> without a dialog.
// N35: the lock holds on every page family that owns a modal: Groups, Settings and the Dashboard.
appTest.describe("open dialog locks the main scroll region @modal", () => {
	// The Dashboard modal fills its list when its own button opens it, so that page is opened through that button.
	const ROUTES = [{ route: "/ui/groups" }, { route: "/ui/settings" }, { route: "/", opener: '[data-bs-target="#dashboard-widgets-modal"]' }];
	for (const [{ route, opener }, width, height] of ROUTES.flatMap((r) => [[r, 390, 844], [r, 1280, 720]])) {
		appTest(`${route} ${width}x${height}`, async ({ browser, browserName, app, adminState }) => {
			const context = await browser.newContext({ viewport: { width, height }, hasTouch: width < 500, storageState: adminState });
			const page = await context.newPage();
			try {
				await page.goto(app.base + route, { waitUntil: "load" });
				const frames = (count) => page.evaluate((n) => new Promise((resolve) => {
					const step = (left) => (left ? requestAnimationFrame(() => step(left - 1)) : resolve());
					step(n);
				}), count);
				const mainTop = () => page.evaluate(() => document.getElementById("main-content").scrollTop);
				await page.evaluate(() => {
					const main = document.getElementById("main-content");
					const filler = document.createElement("div");
					filler.style.height = "4000px";
					main.append(filler);
					main.scrollTop = 300;
				});
				// Widget bodies are level-2 scroll regions with `overscroll-behavior: contain` (DC-SCROLL-4): a wheel over one never
				// reaches <main>, so the control gesture needs a point whose first scroll container is <main> itself.
				const point = await page.evaluate(() => {
					const main = document.getElementById("main-content");
					const box = main.getBoundingClientRect();
					const scrollable = (el) => /(auto|scroll)/.test(getComputedStyle(el).overflowY);
					for (let y = box.top + 24; y < Math.min(box.bottom, innerHeight) - 24; y += 24) {
						for (let x = box.left + 8; x < box.right - 8; x += 24) {
							let el = document.elementFromPoint(x, y);
							if (!el || !main.contains(el)) continue;
							while (el !== main && !scrollable(el)) el = el.parentElement;
							if (el === main) return { x, y };
						}
					}
					return null;
				});
				assert.ok(point, "a point whose scroll container is <main> exists");
				await page.mouse.move(point.x, point.y);
				await page.mouse.wheel(0, 200);
				await expect.poll(mainTop, { message: "control: wheel scrolls <main> without a dialog" }).toBeGreaterThan(300);
				await page.evaluate(() => { document.getElementById("main-content").scrollTop = 300; });
				// shown.bs.modal marks the end of the show transition; Bootstrap ignores Escape/hide() until then.
				const id = await page.evaluate((selector) => new Promise((resolve) => {
					const button = selector ? document.querySelector(selector) : null;
					const modal = button ? document.querySelector(button.dataset.bsTarget) : [...document.querySelectorAll(".modal")].find((m) => m.querySelector("input,button"));
					modal.addEventListener("shown.bs.modal", () => resolve(modal.id), { once: true });
					if (button) button.click();
					else window.bootstrap.Modal.getOrCreateInstance(modal).show();
				}), opener ?? null);
				await page.waitForFunction((i) => document.getElementById(i).classList.contains("show") && !document.querySelector(".modal-backdrop.fade:not(.show)"), id);
				const box = await page.locator(`#${id} .modal-content`).boundingBox();
				await page.mouse.move(3, 3);
				await page.mouse.wheel(0, 500);
				await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
				await page.mouse.wheel(0, 3000);
				// Keys act on the dialog itself: on a focused control Space would activate it instead of scrolling.
				await page.evaluate((i) => document.getElementById(i).focus(), id);
				for (const key of ["PageDown", "Space", "ArrowDown", "End"]) await page.keyboard.press(key);
				if (browserName === "chromium" && width < 500) {
					const cdp = await context.newCDPSession(page);
					for (const x of [width / 2, 5]) {
						await cdp.send("Input.synthesizeScrollGesture", { x, y: height / 2, yDistance: -400, gestureSourceType: "touch", speed: 800 });
					}
				}
				await frames(6);
				assert.equal(await mainTop(), 300, "<main> scrolled behind the open dialog");
				await page.keyboard.press("Escape");
				await page.waitForFunction(() => !document.body.classList.contains("modal-open"));
				assert.equal(await mainTop(), 300, "closing the dialog changed the scroll position of <main>");
			} finally {
				await context.close();
			}
		});
	}
});
// DC-MODAL-8: vertical rhythm. A visible label sits above its control, help and error text directly below it, `form-control-sm` is not mixed with
// regular controls in one dialog, and the primary action dominates the footer while every secondary action keeps a text label.
test("modal forms keep one vertical rhythm, one control size and a dominant primary action @modal", async ({ page, modalBase }) => {
	const findings = [];
	await page.goto(modalBase);
	await page.setViewportSize({ width: 1280, height: 900 });
	const ids = await page.$$eval(".modal.ju-modal--form", (els) => els.map((el) => el.id));
	assert.ok(ids.length >= 4, `form dialogs found: ${ids.length}`);
	for (const id of ids) {
		await open(page, id);
		const out = await page.evaluate((i) => {
			const m = document.getElementById(i);
			const problems = [];
			const box = (el) => el.getBoundingClientRect();
			const visible = (el) => el.offsetParent !== null && !el.closest(".d-none, [hidden]");
			const controls = [...m.querySelectorAll(".modal-body .form-control, .modal-body .form-select")].filter(visible);
			const small = controls.filter((el) => el.classList.contains("form-control-sm") || el.classList.contains("form-select-sm"));
			if (small.length && small.length !== controls.length && !m.classList.contains("ju-modal--compact")) problems.push(`mixes ${small.length} small with ${controls.length - small.length} regular controls`);
			let measured = 0;
			for (const control of controls) {
				const label = control.id ? m.querySelector(`label[for="${control.id}"]`) : null;
				if (label && visible(label) && !label.classList.contains("visually-hidden") && !label.closest(".form-check")) {
					measured += 1;
					if (box(label).bottom > box(control).top + 1) problems.push(`label of #${control.id} is not above its control`);
				}
				for (const token of (control.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean)) {
					const note = document.getElementById(token);
					if (!note || !visible(note) || note.classList.contains("visually-hidden")) continue;
					if (box(note).top < box(control).bottom - 1) problems.push(`help #${token} is not below #${control.id}`);
					else if (box(note).top - box(control).bottom > 24) problems.push(`help #${token} is not directly below #${control.id} (${Math.round(box(note).top - box(control).bottom)}px gap)`);
				}
			}
			// One gap between the stacked blocks of the form: the vertical distance between siblings is a single value per form.
			for (const form of m.querySelectorAll(".ju-modal-form")) {
				const blocks = [...form.children].filter((el) => visible(el) && box(el).height > 0);
				const gaps = blocks.slice(1).map((el, n) => Math.round(box(el).top - box(blocks[n]).bottom));
				if (gaps.length > 1 && Math.max(...gaps) - Math.min(...gaps) > 1) problems.push(`form #${form.id}: unequal gaps ${JSON.stringify(gaps)}`);
				measured += gaps.length;
			}
			const buttons = [...m.querySelectorAll(".modal-footer .btn")];
			const primary = buttons.filter((b) => /\bbtn-(primary|danger|warning)\b/.test(b.className));
			if (primary.length !== 1) problems.push(`${primary.length} primary actions in the footer`);
			for (const b of buttons) if (!b.textContent.replace(/\b(check|close|add|save)\b/g, "").trim() && !b.getAttribute("aria-label")) problems.push("a footer action has no text label");
			for (const b of m.querySelectorAll(".modal-body button.btn")) if (visible(b) && !b.textContent.replace(/[a-z_]+(?=\s|$)/g, (word) => (b.querySelector(".material-icons")?.textContent === word ? "" : word)).trim()) problems.push(`#${b.id || b.className}: a secondary action without a text label`);
			const secondary = [...m.querySelectorAll(".modal-body button.btn")].filter(visible);
			for (const b of secondary) if (/\bbtn-primary\b/.test(b.className)) problems.push(`#${b.id}: a body action is styled as the primary action`);
			if (!measured) problems.push("nothing was measured");
			return problems;
		}, id);
		for (const problem of out) findings.push(`#${id}: ${problem}`);
		await page.keyboard.press("Escape");
		await closed(page, id);
	}
	assert.deepEqual(findings, [], findings.join("\n"));
});
// OR-14 / DC-MEAS-2: the modal checks also run with text enlargement (root font size 200 %) at an unchanged viewport.
//  - 1280x720: the dialog stays inside the viewport, its body scrolls instead of growing past it, and the title, the close button
//    and every footer action stay reachable and unclipped (no loss of content or function).
//  - 390x844: NOT met (measured 2026-11-10, Chromium): the header and the stacked footer outgrow the dialog, so the title is clipped and the
//    footer actions of every dialog leave the visible dialog (DESIGN.md DC-MEAS-2, open). Only what holds today is asserted there:
//    the dialog stays inside the viewport, the page does not scroll sideways and the dialog can still be closed.
for (const [width, height, strict] of [[1280, 720, true], [390, 844, false]]) {
	test(`modals under 200 % text enlargement at ${width}x${height} ${strict ? "lose no content or action" : "stay inside the viewport and closable"} @modal`, async ({ page, modalBase }) => {
		const findings = [];
		await page.setViewportSize({ width, height });
		await page.goto(modalBase);
		await applyTextEnlargement(page, 2);
		const ids = await page.$$eval(".modal", (els) => els.map((el) => el.id));
		assert.ok(ids.length >= 10, `dialogs found: ${ids.length}`);
		for (const id of ids) {
			await open(page, id);
			const out = await page.evaluate(({ i, strictMode }) => {
				const m = document.getElementById(i);
				const problems = [];
				const box = (el) => el.getBoundingClientRect();
				const content = m.querySelector(".modal-content");
				const c = box(content);
				const de = document.documentElement;
				if (parseFloat(getComputedStyle(de).fontSize) < 30) problems.push("text enlargement is not active");
				if (c.left < -1 || c.right > de.clientWidth + 1 || c.top < -1 || c.bottom > innerHeight + 1) problems.push(`dialog exceeds the viewport (${Math.round(c.top)}..${Math.round(c.bottom)} of ${innerHeight})`);
				if (de.scrollWidth > de.clientWidth + 1) problems.push("the page scrolls sideways");
				const close = m.querySelector(".btn-close");
				const cb = box(close);
				if (cb.width < 24 || cb.height < 24 || cb.top < -1 || cb.bottom > innerHeight + 1 || cb.right > de.clientWidth + 1) problems.push("the close button is not reachable");
				if (!strictMode) return problems;
				if (content.scrollWidth > content.clientWidth + 1) problems.push("horizontal overflow");
				const body = m.querySelector(".modal-body");
				if (body.scrollHeight > body.clientHeight + 1 && !["auto", "scroll"].includes(getComputedStyle(body).overflowY)) problems.push("the body overflows without scrolling");
				const title = m.querySelector(".modal-title");
				if (title.scrollWidth > title.clientWidth + 1) problems.push("the title is clipped");
				for (const b of m.querySelectorAll(".modal-footer .btn")) {
					const r = box(b);
					if (r.top < -1 || r.bottom > innerHeight + 1 || r.right > de.clientWidth + 1 || r.left < -1) problems.push(`footer action "${b.textContent.trim()}" is outside the viewport`);
					if (b.scrollWidth > b.clientWidth + 1) problems.push(`footer action "${b.textContent.trim()}" is clipped`);
				}
				const first = body.querySelector("input:not([type=hidden]):not(.visually-hidden), select, textarea");
				if (first && body.scrollHeight > body.clientHeight + 1) {
					first.scrollIntoView({ block: "nearest" });
					const fr = box(first);
					const br = box(body);
					if (fr.bottom < br.top - 1 || fr.top > br.bottom + 1) problems.push("the first field cannot be scrolled into the body");
				}
				return problems;
			}, { i: id, strictMode: strict });
			for (const problem of out) findings.push(`#${id}: ${problem}`);
			await page.keyboard.press("Escape");
			if (!(await closed(page, id))) findings.push(`#${id}: Escape did not close the dialog`);
		}
		assert.deepEqual(findings, [], findings.join("\n"));
	});
}
