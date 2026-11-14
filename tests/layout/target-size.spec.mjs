//
// tests/layout/target-size.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// DC-RESP-6 / D13: on a coarse pointer the shared target token is 44 px and the icon buttons that use it
// measure at least 44x44 CSS px. Chromium derives (pointer: coarse) from isMobile + hasTouch.
import assert from "node:assert/strict";
import { test } from "./fixtures.mjs";
import { waitForVisualStability } from "./harness.mjs";
const PAGES = ["/ui/targets", "/ui/settings"];
for (const coarse of [true, false]) {
	for (const route of PAGES) {
		test(`icon buttons honour the ${coarse ? "44" : "24"} px target token on ${route} @target-size`, async ({ browser, app, adminState }) => {
			const context = await browser.newContext({
				viewport: { width: 390, height: 844 }, isMobile: coarse, hasTouch: coarse, storageState: adminState,
			});
			const page = await context.newPage();
			try {
				await page.goto(`${app.base}${route}`, { waitUntil: "load" });
				await waitForVisualStability(page);
				const result = await page.evaluate(() => {
					const root = getComputedStyle(document.documentElement);
					const probe = document.createElement("div");
					probe.style.cssText = "position:absolute;visibility:hidden;width:var(--ui-target-min)";
					document.body.append(probe);
					const token = probe.getBoundingClientRect().width;
					probe.remove();
					const small = [...document.querySelectorAll(".btn-icon")].filter((el) => el.offsetParent).map((el) => {
						const r = el.getBoundingClientRect();
						return { cls: el.className, w: r.width, h: r.height };
					});
					return { coarse: matchMedia("(pointer: coarse)").matches, token, buttons: small, raw: root.getPropertyValue("--ui-target-min").trim() };
				});
				assert.equal(result.coarse, coarse, "pointer emulation is in effect");
				assert.equal(result.token, coarse ? 44 : 24, `--ui-target-min resolves to ${result.raw}`);
				for (const button of result.buttons) {
					const min = coarse ? 44 : 24;
					assert.equal(button.w >= min - 0.5 && button.h >= min - 0.5, true, `.btn-icon ${button.cls} is ${button.w}x${button.h}, expected >= ${min}`);
				}
			} finally {
				await context.close();
			}
		});
	}
}
