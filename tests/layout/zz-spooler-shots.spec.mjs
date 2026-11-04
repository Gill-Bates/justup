//
// tests/layout/zz-spooler-shots.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import fs from "node:fs";
import { test } from "./fixtures.mjs";
const OUT = process.env.SHOTS;
const ENTRIES = [
	{ id: 1, status: "sent", channel_type: "email", notification_type: "alert", retry_count: 0, updated_at: new Date().toISOString(), recipient_name: "Martin", recipient_address: "m@example.org", target_name: "Example Web", target_id: 1, last_error: null },
	{ id: 2, status: "sent", channel_type: "signal", notification_type: "alert", retry_count: 0, updated_at: new Date().toISOString(), recipient_name: "Tobias", recipient_address: "+4915112345678", target_name: "Example Web", target_id: 1, last_error: null },
	{ id: 3, status: "failed", channel_type: "email", notification_type: "test", retry_count: 3, updated_at: new Date().toISOString(), recipient_name: "Martin", recipient_address: "m@example.org", target_name: null, target_id: null, last_error: "Connection refused" },
];
for (const vp of [{ n: "1280x720", w: 1280, h: 720 }, { n: "390x844", w: 390, h: 844 }]) {
	for (const theme of ["light", "dark"]) {
		test(`spooler shots ${vp.n} ${theme}`, async ({ browser, app, adminState }) => {
			const ctx = await browser.newContext({ viewport: { width: vp.w, height: vp.h }, storageState: adminState });
			const page = await ctx.newPage();
			const errors = [];
			page.on("pageerror", (e) => errors.push(String(e)));
			await page.route("**/settings/notification-queue/log**", (r) => r.fulfill({ json: { entries: ENTRIES, total: 3 } }));
			await page.goto(app.base + "/ui/settings", { waitUntil: "load" });
			await page.evaluate((t) => document.documentElement.setAttribute("data-bs-theme", t), theme);
			await page.click("#spooler-tab");
			await page.waitForSelector("#spooler-log-body tr td[data-label=Channel]", { timeout: 15000 });
			await page.waitForTimeout(500);
			const info = await page.evaluate(() => {
				const cells = [...document.querySelectorAll("#spooler-log-body td[data-label=Channel]")];
				const th = document.querySelector("#spooler-log-table thead th:nth-child(2)");
				const de = document.documentElement;
				return {
					channel: cells.map((c) => ({ text: c.textContent.trim(), visibleText: c.innerText.trim(), title: c.querySelector("[title]")?.title, w: Math.round(c.getBoundingClientRect().width), align: getComputedStyle(c).textAlign })),
					statusBadges: [...document.querySelectorAll("#spooler-log-body td[data-label=Status] .badge")].map((b) => b.textContent.trim() + "|" + (b.title || "")),
					filterOptions: [...document.querySelectorAll("#spooler-filter-status option")].map((o) => o.textContent.trim()),
					statTiles: [...document.querySelectorAll("#spooler-stat-sent")].map((e) => e.parentElement.parentElement.textContent.replace(/\s+/g, " ").trim()),
					headerW: th ? Math.round(th.getBoundingClientRect().width) : null,
					overflow: de.scrollWidth > de.clientWidth + 1,
				};
			});
			console.log(`INFO ${vp.n} ${theme} ` + JSON.stringify(info) + (errors.length ? " ERR " + errors.join("|") : ""));
			await page.locator("#spooler-pane").screenshot({ path: `${OUT}/spooler-${theme}-${vp.n}.png` });
			await ctx.close();
		});
	}
}
