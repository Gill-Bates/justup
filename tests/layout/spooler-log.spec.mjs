//
// tests/layout/spooler-log.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Delivery Queue Log (DESIGN.md, table contract): a narrow container shows one card per entry with
// label/value rows, a wide one the table. The channel is an icon with an accessible name in the table
// and icon plus visible name in the card.
import assert from "node:assert/strict";
import { test } from "./fixtures.mjs";

const NOW = new Date().toISOString();
const ENTRIES = [
	{ id: 1, status: "sent", channel_type: "email", notification_type: "alert", retry_count: 0, updated_at: NOW, recipient_name: "Martin", recipient_address: "m@example.org", target_name: "Example Web", target_id: 1, last_error: null },
	{ id: 2, status: "sent", channel_type: "signal", notification_type: "alert", retry_count: 0, updated_at: NOW, recipient_name: "Tobias", recipient_address: "+4915112345678", target_name: "Example Web", target_id: 1, last_error: null },
	{ id: 3, status: "failed", channel_type: "email", notification_type: "test", retry_count: 3, updated_at: NOW, recipient_name: "Martin", recipient_address: "m@example.org", target_name: null, target_id: null, last_error: "Connection refused by a server with a rather long error text" },
];
const VIEWPORTS = [
	{ name: "1280x720", width: 1280, height: 720, card: false },
	{ name: "390x844", width: 390, height: 844, card: true },
	{ name: "320x640", width: 320, height: 640, card: true },
];

for (const viewport of VIEWPORTS) {
	for (const theme of ["light", "dark"]) {
		test(`delivery queue log ${viewport.card ? "cards" : "table"} at ${viewport.name} ${theme} @spooler-log`, async ({ browser, app, adminState }) => {
			const context = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height }, storageState: adminState });
			const page = await context.newPage();
			const errors = [];
			page.on("pageerror", (error) => errors.push(String(error)));
			await page.route("**/settings/notification-queue/log**", (route) => route.fulfill({ json: { entries: ENTRIES, total: 3 } }));
			try {
				await page.goto(app.base + "/ui/settings", { waitUntil: "load" });
				await page.evaluate((mode) => document.documentElement.setAttribute("data-bs-theme", mode), theme);
				await page.click("#spooler-tab");
				await page.waitForSelector("#spooler-log-body td[data-label=Channel]", { timeout: 15000 });
				await page.waitForTimeout(400);
				const m = await page.evaluate(() => {
					const table = document.getElementById("spooler-log-table");
					const row = table.querySelector("tbody tr");
					const cell = row.querySelector("td[data-label=Channel]");
					const name = cell.querySelector(".visually-hidden");
					const nameBox = name.getBoundingClientRect();
					const viewportEl = table.closest(".ju-table-viewport");
					const de = document.documentElement;
					return {
						rowDisplay: getComputedStyle(row).display,
						label: getComputedStyle(cell, "::before").content,
						nameWidth: nameBox.width,
						nameHeight: nameBox.height,
						nameText: name.textContent.trim(),
						cells: [...row.children].map((c) => c.dataset.label),
						pageOverflow: de.scrollWidth > de.clientWidth + 1,
						regionOverflow: viewportEl.scrollWidth > viewportEl.clientWidth + 1,
						icon: cell.querySelector(".material-icons")?.getAttribute("aria-hidden"),
						channelColumnWidth: cell.getBoundingClientRect().width,
					};
				});
				assert.equal(m.pageOverflow, false, "the page scrolls horizontally");
				assert.ok(m.cells.every(Boolean), `every cell carries its data-label: ${m.cells}`);
				assert.equal(m.icon, "true", "the channel icon is decorative");
				if (viewport.card) {
					assert.equal(m.rowDisplay, "block", "narrow container: one card per entry");
					assert.equal(m.label, '"Channel"', "the card row is labelled by data-label");
					assert.ok(m.nameWidth > 20 && m.nameHeight > 8, `the channel name is visible in the card (${m.nameWidth}x${m.nameHeight})`);
					assert.equal(m.regionOverflow, false, "the cards fit without horizontal scrolling");
				} else {
					assert.equal(m.rowDisplay, "table-row", "wide container: the table");
					assert.ok(m.nameWidth <= 1 && m.nameHeight <= 1, "the channel name is not visible in the table");
					assert.ok(m.channelColumnWidth <= 64, `the channel column stays narrow (${m.channelColumnWidth}px)`);
				}
				assert.deepEqual(errors, []);
			} finally {
				await context.close();
			}
		});
	}
}
