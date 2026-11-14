//
// tests/layout/smtp-link-field.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Mail template editor, "Insert link": the address is typed into a field of the editor (no native prompt),
// a blocked or empty address is a field error with focus on the field and nothing inserted, and a
// valid address becomes a link (DESIGN.md DC-FORM-6..10, section 6.2).
import assert from "node:assert/strict";
import { test } from "./fixtures.mjs";

const VIEWPORTS = [{ name: "1280x720", width: 1280, height: 720 }, { name: "390x844", width: 390, height: 844 }];

for (const viewport of VIEWPORTS) {
	for (const theme of ["light", "dark"]) {
		test(`insert link reports a blocked address at the field at ${viewport.name} ${theme} @smtp-link-field`, async ({ browser, app, adminState }) => {
			const context = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height }, storageState: adminState });
			const page = await context.newPage();
			const native = [];
			page.on("dialog", (dialog) => { native.push(dialog.type()); dialog.dismiss(); });
			const errors = [];
			page.on("pageerror", (error) => errors.push(String(error)));
			try {
				await page.goto(app.base + "/ui/settings", { waitUntil: "load" });
				await page.evaluate((mode) => document.documentElement.setAttribute("data-bs-theme", mode), theme);
				await page.click("#smtp-tab");
				await page.waitForSelector("#btn-mode-edit", { timeout: 15000 });
				await page.click("#btn-mode-edit");
				await page.waitForSelector("#wysiwyg-container .wysiwyg-toolbar", { state: "visible" });
				await page.waitForFunction(() => document.getElementById("wysiwyg_frame")?.contentDocument?.body?.isContentEditable);

				// Select some text in the editor, then open the link field.
				await page.evaluate(() => {
					const doc = document.getElementById("wysiwyg_frame").contentDocument;
					doc.body.innerHTML = "<p>Open the dashboard</p>";
					const range = doc.createRange();
					range.selectNodeContents(doc.body.firstChild);
					const selection = doc.getSelection();
					selection.removeAllRanges();
					selection.addRange(range);
				});
				await page.click('.wysiwyg-toolbar [data-cmd="createLink"]');
				const input = page.locator("#wysiwyg-link-url");
				await input.waitFor({ state: "visible" });
				assert.equal(await page.evaluate(() => document.activeElement.id), "wysiwyg-link-url", "focus is in the field");

				// A script address: field error, focus stays on the field, nothing is inserted.
				await input.fill("javascript:alert(1)");
				await page.click("#wysiwyg-link-apply");
				const state = await page.evaluate(() => {
					const field = document.getElementById("wysiwyg-link-url");
					const error = document.getElementById("wysiwyg-link-url-field-error");
					const doc = document.getElementById("wysiwyg_frame").contentDocument;
					return {
						invalid: field.getAttribute("aria-invalid"),
						described: field.getAttribute("aria-describedby"),
						errorText: error?.textContent.trim(),
						errorVisible: Boolean(error && error.getClientRects().length),
						focus: document.activeElement.id,
						links: doc.querySelectorAll("a").length,
						overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
					};
				});
				assert.equal(state.invalid, "true");
				assert.ok(state.described?.includes("wysiwyg-link-url-field-error"));
				assert.match(state.errorText, /not allowed/i);
				assert.equal(state.errorVisible, true, "the error text is visible");
				assert.equal(state.focus, "wysiwyg-link-url");
				assert.equal(state.links, 0, "nothing was inserted");
				assert.equal(state.overflow, false);

				// Editing clears the error; a valid address is inserted on Enter.
				await input.fill("https://example.org/dash");
				assert.equal(await input.getAttribute("aria-invalid"), null, "editing clears the error");
				await input.press("Enter");
				const link = await page.evaluate(() => {
					const a = document.getElementById("wysiwyg_frame").contentDocument.querySelector("a");
					return { href: a?.getAttribute("href"), text: a?.textContent, rowHidden: document.getElementById("wysiwyg-link-row").hidden };
				});
				assert.deepEqual(link, { href: "https://example.org/dash", text: "Open the dashboard", rowHidden: true });

				// Escape closes the field and hands focus back to the toolbar button.
				await page.click('.wysiwyg-toolbar [data-cmd="createLink"]');
				await page.keyboard.press("Escape");
				assert.equal(await page.evaluate(() => document.activeElement?.getAttribute("data-cmd")), "createLink");
				assert.deepEqual(native, [], "a native dialog appeared");
				assert.deepEqual(errors, []);
			} finally {
				await context.close();
			}
		});
	}
}

test("inline raster data:image survives the editor round trip, active content is dropped @smtp-link-field", async ({ browser, app, adminState }) => {
	const context = await browser.newContext({ storageState: adminState });
	const page = await context.newPage();
	try {
		await page.goto(app.base + "/ui/settings", { waitUntil: "load" });
		await page.click("#smtp-tab");
		await page.waitForSelector("#btn-mode-html", { timeout: 15000 });
		await page.click("#btn-mode-html");
		const png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==";
		await page.evaluate((src) => {
			document.getElementById("email_html_source").value = `<p><img id="ok" src="${src}"><img id="svg" src="data:image/svg+xml;base64,PHN2Zz4="><img id="js" src="javascript:alert(1)"><a id="h" href="data:text/html;base64,PGI+">x</a></p>`;
		}, png);
		await page.click("#btn-mode-edit");
		await page.waitForFunction(() => document.getElementById("wysiwyg_frame")?.contentDocument?.body?.isContentEditable);
		await page.click("#btn-mode-html");
		const saved = await page.evaluate(() => {
			const doc = new DOMParser().parseFromString(document.getElementById("email_html_source").value, "text/html");
			const attr = (id, name) => doc.getElementById(id)?.getAttribute(name);
			return { ok: attr("ok", "src"), svg: attr("svg", "src"), js: attr("js", "src"), href: attr("h", "href") };
		});
		assert.equal(saved.ok, png, "the inline PNG survives");
		assert.equal(saved.svg, null);
		assert.equal(saved.js, null);
		assert.equal(saved.href, null);
	} finally {
		await context.close();
	}
});
