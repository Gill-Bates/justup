//
// tests/layout/settings-tab-press.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// A mouse press on a settings tab must not scroll the strip under the pointer: the click would then end on another
// element and the tab never opens (seen as a flaky `settings-signal`/`settings-smtp-source` audit in WebKit).
import assert from "node:assert/strict";
import { test } from "./fixtures.mjs";

for (const tab of ["smtp", "signal"]) {
	test(`pressing the ${tab} tab slowly still opens it @settings-tab-press`, async ({ browser, app, adminState }) => {
		const context = await browser.newContext({ viewport: { width: 1180, height: 820 }, storageState: adminState });
		const page = await context.newPage();
		try {
			await page.goto(app.base + "/ui/settings", { waitUntil: "load" });
			await page.waitForFunction(() => document.querySelector("#general-pane")?.children.length > 0);
			await page.locator(`#${tab}-tab`).scrollIntoViewIfNeeded();
			await page.waitForTimeout(500);
			const box = await page.locator(`#${tab}-tab`).boundingBox();
			await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
			await page.mouse.down();
			// Long enough for a smooth scroll started by the press to move the strip.
			await page.waitForTimeout(500);
			await page.mouse.up();
			await page.waitForFunction((id) => document.getElementById(id)?.classList.contains("active"), `${tab}-tab`, { timeout: 3000 });
			assert.equal(await page.getAttribute(`#${tab}-tab`, "aria-selected"), "true");
		} finally {
			await context.close();
		}
	});
}
