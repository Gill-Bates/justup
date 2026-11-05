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
		// 390px: the strip overflows, so a scroll triggered by the press has room to move it.
		const context = await browser.newContext({ viewport: { width: 390, height: 844 }, storageState: adminState });
		const page = await context.newPage();
		try {
			await page.goto(app.base + "/ui/settings", { waitUntil: "load" });
			await page.waitForFunction(() => document.querySelector("#general-pane")?.children.length > 0);
			const strip = page.locator("#settingsTabs");
			const overflow = await strip.evaluate((el) => el.scrollWidth - el.clientWidth);
			assert.ok(overflow > 0, `the tab strip must overflow for this test to mean anything (${overflow})`);
			// Park the strip so the target tab is only partly visible: the position revealTab would move it from.
			const parked = await page.evaluate((id) => {
				const el = document.getElementById("settingsTabs");
				const tab = document.getElementById(id);
				el.style.scrollBehavior = "auto";
				el.scrollLeft = Math.max(0, tab.offsetLeft + tab.offsetWidth * 0.6 - el.clientWidth);
				const box = tab.getBoundingClientRect();
				const bar = el.getBoundingClientRect();
				return { left: el.scrollLeft, partial: box.right > bar.right + 1 || box.left < bar.left - 1 };
			}, `${tab}-tab`);
			assert.ok(parked.partial, `the ${tab} tab must be partly outside the strip before the press`);
			await page.waitForFunction(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve(true)))));
			const before = await strip.evaluate((el) => el.scrollLeft);
			const box = await page.locator(`#${tab}-tab`).boundingBox();
			const bar = await strip.boundingBox();
			// Press inside the visible part of the tab.
			const x1 = Math.max(box.x, bar.x), x2 = Math.min(box.x + box.width, bar.x + bar.width);
			await page.mouse.move((x1 + x2) / 2, box.y + box.height / 2);
			await page.mouse.down();
			// Let a press-triggered smooth scroll start and run for at least one frame pair, then check it stayed put.
			await page.evaluate(() => new Promise((resolve) => {
				let frames = 0;
				const tick = () => (++frames >= 30 ? resolve() : requestAnimationFrame(tick));
				tick();
			}));
			assert.equal(await strip.evaluate((el) => el.scrollLeft), before, "the strip moved under the pressed pointer");
			await page.mouse.up();
			await page.waitForFunction((id) => document.getElementById(id)?.classList.contains("active"), `${tab}-tab`, { timeout: 3000 });
			assert.equal(await page.getAttribute(`#${tab}-tab`, "aria-selected"), "true");
		} finally {
			await context.close();
		}
	});
}
