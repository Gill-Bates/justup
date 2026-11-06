//
// tests/layout/reset-confirm.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// "Reset metrics" and "Delete target" confirm in the shared dialog of the design contract (never a
// native popup): the target and the consequence are named, Cancel has the first focus, Escape and Cancel
// send nothing, Confirm sends exactly one request, focus returns to the trigger, and the table stays
// operable after a live update.
import assert from "node:assert/strict";
import { test } from "./fixtures.mjs";

const VIEWPORTS = [{ name: "1280x720", width: 1280, height: 720 }, { name: "390x844", width: 390, height: 844 }];

async function open(browser, app, adminState, viewport, theme, path) {
	const context = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height }, storageState: adminState });
	const page = await context.newPage();
	const state = { nativeDialogs: 0, posts: [] };
	page.on("dialog", (dialog) => { state.nativeDialogs += 1; dialog.dismiss(); });
	await page.route("**/targets/*/reset", (route) => {
		state.posts.push(route.request().url());
		return route.fulfill({ status: 204 });
	});
	await page.goto(app.base + path, { waitUntil: "load" });
	await page.evaluate((mode) => document.documentElement.setAttribute("data-bs-theme", mode), theme);
	return { context, page, state };
}

const modalState = (page) => page.evaluate(() => {
	const modal = document.getElementById("ju-confirm-modal");
	return {
		shown: Boolean(modal?.classList.contains("show")),
		text: modal?.textContent.replace(/\s+/g, " ").trim() || "",
		focus: document.activeElement?.id || document.activeElement?.className || "",
		labelledby: modal?.getAttribute("aria-labelledby"),
		describedby: modal?.getAttribute("aria-describedby"),
		locked: document.body.classList.contains("modal-open"),
		pageOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
	};
});
const settled = async (page, shown) => {
	await page.waitForFunction((want) => {
		const modal = document.getElementById("ju-confirm-modal");
		const isShown = Boolean(modal?.classList.contains("show")) && !modal.classList.contains("showing");
		return want ? isShown && document.activeElement?.id === "ju-confirm-cancel" : !modal || (!modal.classList.contains("show") && !document.querySelector(".modal-backdrop"));
	}, shown, { timeout: 10000 });
};

for (const viewport of VIEWPORTS) {
	for (const theme of ["light", "dark"]) {
		test(`targets list: reset asks in the shared dialog at ${viewport.name} ${theme} @reset-confirm`, async ({ browser, app, adminState }) => {
			const { context, page, state } = await open(browser, app, adminState, viewport, theme, "/ui/targets");
			try {
				await page.waitForSelector('#targets-table button[title="Reset Metrics"]', { timeout: 20000 });
				const trigger = page.locator('#targets-table tr:has-text("wiedenest.de") button[title="Reset Metrics"]');
				const triggerId = await trigger.evaluate((el) => { el.id = el.id || "reset-trigger"; return el.id; });

				// Open: names the target and the consequence; Cancel holds the first focus; the page behind is locked.
				await trigger.click();
				await settled(page, true);
				let m = await modalState(page);
				assert.ok(m.text.includes("wiedenest.de"), `target name missing: ${m.text}`);
				assert.ok(/deleted permanently/.test(m.text) && /target itself stays/.test(m.text), `consequence missing: ${m.text}`);
				assert.equal(m.focus, "ju-confirm-cancel");
				assert.ok(m.labelledby && m.describedby);
				assert.equal(m.locked, true, "background scroll is locked while the dialog is open");
				assert.equal(m.pageOverflow, false);

				// Cancel and Escape send nothing and return focus to the trigger.
				await page.click("#ju-confirm-cancel");
				await settled(page, false);
				assert.equal(await page.evaluate(() => document.activeElement?.id), triggerId, "focus did not return after Cancel");
				await trigger.click();
				await settled(page, true);
				await page.keyboard.press("Escape");
				await settled(page, false);
				assert.equal(await page.evaluate(() => document.activeElement?.id), triggerId, "focus did not return after Escape");
				assert.equal(state.posts.length, 0, "a cancelled dialog sent a request");

				// A live update replaces the table; the new button works the same way. The old node is marked
				// so a no-op refresh cannot pass for a replaced row.
				const oldHref = await trigger.getAttribute("hx-post");
				await trigger.evaluate((el) => { el.dataset.stale = "1"; });
				await page.evaluate(() => window.htmx.trigger("#targets-table", "refresh"));
				await page.waitForFunction(() => !document.querySelector('#targets-table [data-stale]'), null, { timeout: 10000 });
				const fresh = page.locator('#targets-table tr:has-text("wiedenest.de") button[title="Reset Metrics"]');
				assert.equal(await fresh.getAttribute("hx-post"), oldHref, "the refreshed button targets the same endpoint");
				await fresh.click();
				await settled(page, true);

				// Confirm: exactly one request, to the clicked target's reset endpoint.
				const request = page.waitForRequest((r) => r.url().endsWith(oldHref));
				await page.click("#ju-confirm-ok");
				await request;
				await settled(page, false);
				assert.equal(state.posts.length, 1, "confirm must send exactly one request");
				assert.ok(state.posts[0].endsWith(oldHref), `the request went to ${state.posts[0]}`);
				assert.equal(state.nativeDialogs, 0, "a native popup appeared");
			} finally {
				await context.close();
			}
		});

		test(`target detail: reset asks in the shared dialog at ${viewport.name} ${theme} @reset-confirm`, async ({ browser, app, adminState }) => {
			const { context, page, state } = await open(browser, app, adminState, viewport, theme, "/ui/targets/2");
			try {
				const phone = viewport.width < 576;
				await page.waitForSelector(".target-detail-actions", { timeout: 20000 });
				const reset = page.locator('button[hx-post$="/reset"]').first();
				if (phone || !(await reset.isVisible())) await page.click("#target-more-toggle");
				await reset.click();
				await settled(page, true);
				const m = await modalState(page);
				assert.ok(/deleted permanently/.test(m.text) && /target itself stays/.test(m.text), m.text);
				assert.equal(m.focus, "ju-confirm-cancel");
				await page.keyboard.press("Escape");
				await settled(page, false);
				assert.equal(state.posts.length, 0);
				assert.equal(state.nativeDialogs, 0, "a native popup appeared");
			} finally {
				await context.close();
			}
		});
	}
}
