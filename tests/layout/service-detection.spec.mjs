//
// tests/layout/service-detection.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// "Detect services" on the Network tab of Edit Target: the preview groups service changes and
// version-only changes and stores nothing; "Apply changes" asks in the shared dialog (never a native
// popup); Cancel and Escape send nothing; Apply sends exactly one confirmation; a failed preview shows
// the error and offers no Apply. Chromium and WebKit, light and dark, desktop and phone.
import assert from "node:assert/strict";
import { test } from "./fixtures.mjs";
const VIEWPORTS = [{ name: "1280x720", width: 1280, height: 720 }, { name: "390x844", width: 390, height: 844 }];
const TOKEN = "a".repeat(32);
const CHANGES = [
	{ kind: "service_changed", protocol: "tcp", port: 25, previous_name: "smtp", previous_product: "Postfix", previous_version: "3.8", name: "http", product: "nginx", version: "1" },
	{ kind: "identified", protocol: "udp", port: 53, previous_name: null, previous_product: null, previous_version: null, name: "domain", product: "BIND", version: "9" },
	{ kind: "version_changed", protocol: "tcp", port: 22, previous_name: "ssh", previous_product: "OpenSSH", previous_version: "8.0", name: "ssh", product: "OpenSSH", version: "9.0" },
];
async function open(browser, app, adminState, viewport, theme, previewReply) {
	const context = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height }, storageState: adminState });
	const page = await context.newPage();
	const state = { nativeDialogs: 0, previews: 0, confirms: [] };
	page.on("dialog", (dialog) => { state.nativeDialogs += 1; dialog.dismiss(); });
	await page.route("**/network-scans/services/*/preview", async (route) => {
		state.previews += 1;
		await route.fulfill(previewReply);
	});
	await page.route("**/network-scans/services/*/confirm", async (route) => {
		state.confirms.push(route.request().postDataJSON());
		await route.fulfill({ status: 200, json: CHANGES });
	});
	await page.goto(`${app.base}/ui/targets/1/edit`, { waitUntil: "load" });
	await page.evaluate((mode) => document.documentElement.setAttribute("data-bs-theme", mode), theme);
	await page.click("#target-network-tab");
	await page.waitForSelector("#service-detection [data-service-action=preview]", { state: "visible" });
	return { context, page, state };
}
const view = (page) => page.evaluate(() => {
	const root = document.getElementById("service-detection");
	const visible = (el) => Boolean(el) && !el.hidden && el.getClientRects().length > 0;
	const list = (kind) => [...root.querySelectorAll(`[data-service-list="${kind}"] li`)].map((li) => li.textContent);
	const apply = root.querySelector('[data-service-action="confirm"]');
	return {
		status: root.querySelector("[data-service-status]").textContent,
		error: visible(root.querySelector("[data-service-error]")) ? root.querySelector("[data-service-error]").textContent : "",
		serviceGroup: visible(root.querySelector('[data-service-group="service"]')),
		versionGroup: visible(root.querySelector('[data-service-group="version"]')),
		serviceItems: list("service"),
		versionItems: list("version"),
		applyVisible: visible(apply),
		applyName: apply.textContent.trim(),
		focusOnApply: document.activeElement === apply,
		busy: root.getAttribute("aria-busy"),
		pageOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
		rootOverflow: root.scrollWidth > root.clientWidth + 1,
	};
});
const settled = async (page, shown) => {
	await page.waitForFunction((want) => {
		const modal = document.getElementById("ju-confirm-modal");
		const isShown = Boolean(modal?.classList.contains("show")) && !modal.classList.contains("showing");
		return want ? isShown && document.activeElement?.id === "ju-confirm-cancel" : !modal || (!modal.classList.contains("show") && !document.querySelector(".modal-backdrop"));
	}, shown, { timeout: 10000 });
};
const OK = { status: 200, json: { token: TOKEN, changes: CHANGES } };
for (const viewport of VIEWPORTS) {
	for (const theme of ["light", "dark"]) {
		test(`service detection: preview groups changes and stores nothing at ${viewport.name} ${theme} @service-detection`, async ({ browser, app, adminState }) => {
			const { context, page, state } = await open(browser, app, adminState, viewport, theme, OK);
			try {
				let v = await view(page);
				assert.equal(v.applyVisible, false, "Apply is offered before any preview");
				await page.click('#service-detection [data-service-action="preview"]');
				await page.waitForFunction(() => document.querySelector("[data-service-status]").textContent === "3 changes");
				v = await view(page);
				assert.deepEqual(v.serviceItems, ["TCP:25 smtp / Postfix → http / nginx", "UDP:53 unknown → domain / BIND"]);
				assert.deepEqual(v.versionItems, ["TCP:22 ssh / OpenSSH 8.0 → 9.0"]);
				assert.ok(v.serviceGroup && v.versionGroup && v.applyVisible);
				assert.equal(v.busy, "false");
				assert.equal(v.pageOverflow, false, "the preview overflows the page");
				assert.equal(v.rootOverflow, false, "the preview overflows its section");
				assert.equal(state.previews, 1);
				assert.equal(state.confirms.length, 0, "the preview alone must not confirm");
				assert.equal(state.nativeDialogs, 0);
			} finally {
				await context.close();
			}
		});
		test(`service detection: Apply asks in the shared dialog, Cancel and Escape send nothing at ${viewport.name} ${theme} @service-detection`, async ({ browser, app, adminState }) => {
			const { context, page, state } = await open(browser, app, adminState, viewport, theme, OK);
			try {
				await page.click('#service-detection [data-service-action="preview"]');
				await page.waitForFunction(() => document.querySelector("[data-service-status]").textContent === "3 changes");
				await page.click('#service-detection [data-service-action="confirm"]');
				await settled(page, true);
				const text = await page.evaluate(() => document.getElementById("ju-confirm-modal").textContent.replace(/\s+/g, " ").trim());
				assert.ok(text.includes("Apply service changes") && text.includes("3 stored entries are updated."), text);
				await page.click("#ju-confirm-cancel");
				await settled(page, false);
				assert.equal((await view(page)).focusOnApply, true, "focus did not return to Apply after Cancel");
				await page.click('#service-detection [data-service-action="confirm"]');
				await settled(page, true);
				await page.keyboard.press("Escape");
				await settled(page, false);
				assert.equal(state.confirms.length, 0, "a dismissed dialog sent a confirmation");
				assert.equal((await view(page)).applyVisible, true, "the preview was dropped by a dismissal");
				assert.equal(state.nativeDialogs, 0, "a native popup appeared");
			} finally {
				await context.close();
			}
		});
		test(`service detection: Apply sends exactly one confirmation with the token at ${viewport.name} ${theme} @service-detection`, async ({ browser, app, adminState }) => {
			const { context, page, state } = await open(browser, app, adminState, viewport, theme, OK);
			try {
				await page.click('#service-detection [data-service-action="preview"]');
				await page.waitForFunction(() => document.querySelector("[data-service-status]").textContent === "3 changes");
				await page.click('#service-detection [data-service-action="confirm"]');
				await settled(page, true);
				// A double click on the dialog's Apply must still confirm once.
				await page.dblclick("#ju-confirm-ok");
				await page.waitForLoadState("load");
				await page.waitForTimeout(800); // deliberate window: proves no second confirmation follows the double click
				assert.deepEqual(state.confirms, [{ token: TOKEN }]);
				assert.equal(state.nativeDialogs, 0);
			} finally {
				await context.close();
			}
		});
		test(`service detection: a failed preview shows the error and no Apply at ${viewport.name} ${theme} @service-detection`, async ({ browser, app, adminState }) => {
			const { context, page, state } = await open(browser, app, adminState, viewport, theme, { status: 429, json: { detail: "Nmap is busy" } });
			try {
				await page.click('#service-detection [data-service-action="preview"]');
				await page.waitForFunction(() => !document.querySelector("[data-service-error]").hidden);
				const v = await view(page);
				assert.equal(v.error, "Nmap is busy");
				assert.equal(v.applyVisible, false);
				assert.equal(v.serviceGroup || v.versionGroup, false);
				assert.equal(v.pageOverflow, false);
				assert.equal(state.confirms.length, 0);
			} finally {
				await context.close();
			}
		});
	}
}
test("service detection: no changes offers no Apply @service-detection", async ({ browser, app, adminState }) => {
	const { context, page } = await open(browser, app, adminState, VIEWPORTS[0], "light", { status: 200, json: { token: null, changes: [] } });
	try {
		await page.click('#service-detection [data-service-action="preview"]');
		await page.waitForFunction(() => document.querySelector("[data-service-status]").textContent === "No changes");
		const v = await view(page);
		assert.equal(v.applyVisible, false);
		assert.equal(v.serviceGroup || v.versionGroup, false);
	} finally {
		await context.close();
	}
});
