//
// tests/layout/notif-dropdown.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { mkdirSync } from "node:fs";
import path from "node:path";
import { test } from "@playwright/test";
import { AUDIT, FIXTURES, PAGE, STYLES, THEMES, VIEWPORTS } from "./notif-dropdown.mjs";

const shots = process.env.JUSTUP_NOTIFICATION_DROPDOWN_SHOTS;
if (shots) mkdirSync(shots, { recursive: true });

for (const viewport of VIEWPORTS) {
	for (const theme of THEMES) {
		test(`notification dropdown time visibility at ${viewport.name} ${theme} @notification-dropdown`, async ({ page, browserName }) => {
			await page.setViewportSize({ width: viewport.w, height: viewport.h });
			await page.setContent(PAGE(STYLES), { waitUntil: "load" });
			await page.evaluate(() => document.fonts && document.fonts.ready);
			await page.evaluate((value) => document.documentElement.setAttribute("data-bs-theme", value), theme);
			await page.evaluate((items) => {
				const notifications = window.JustUpNotifications;
				notifications.renderPreview(document.getElementById("notif-pop-list"), { items }, document, document.getElementById("notif-bell"));
			}, FIXTURES);
			await page.waitForTimeout(80);

			const result = await page.evaluate(`(${AUDIT.toString()})(${JSON.stringify(FIXTURES)})`);
			assert.deepEqual(result.issues, [], `${browserName}/${viewport.name}/${theme} notification dropdown findings`);

			if (shots && browserName === "chromium") {
				await page.screenshot({ path: path.join(shots, `after-${viewport.name}-${theme}.png`) });
				await page.evaluate(() => {
					document.querySelectorAll("#notif-pop-list .ju-notif-pop-item").forEach((item) => {
						const detail = item.querySelector(".ju-notif-pop-detail");
						const context = item.querySelector(".ju-notif-pop-context");
						const time = item.querySelector(".ju-notif-pop-time");
						if (!detail) return;
						detail.style.display = "block";
						detail.style.whiteSpace = "nowrap";
						detail.style.overflow = "hidden";
						detail.style.textOverflow = "ellipsis";
						detail.textContent = [context?.textContent || "", time?.textContent || ""].filter(Boolean).join(" \u00b7 ");
					});
				});
				await page.waitForTimeout(40);
				await page.screenshot({ path: path.join(shots, `before-${viewport.name}-${theme}.png`) });
			}
		});
	}
}
