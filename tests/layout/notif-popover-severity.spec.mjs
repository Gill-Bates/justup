//
// tests/layout/notif-popover-severity.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// DC-NOTIF-3/-6: popover severity is a status dot plus a word and independent of the unread marker.
import assert from "node:assert/strict";
import { test } from "@playwright/test";
import { PAGE, STYLES, THEMES } from "./notif-dropdown.mjs";
const VIEWS = [
	{ name: "320x640", w: 320, h: 640, dsf: 1 },
	{ name: "effective-zoom-200pct", w: 640, h: 360, dsf: 2 }, // viewport proxy: 640x360 CSS px at DPR 2
	{ name: "1280x800", w: 1280, h: 800, dsf: 1 },
];
const WORDS = { error: "Error", warning: "Warning", success: "Success", info: "Info" };
const SHAPES = { error: "danger", warning: "warning", success: "success", info: "unknown" };
const item = (id, severity, is_read) => ({ id, title: `${severity} ${is_read ? "read" : "unread"}`, body: "Detail", actor: "", severity, category: "activity", is_read, created_at: new Date().toISOString() });
for (const view of VIEWS) {
	for (const theme of THEMES) {
		test(`notification popover severity at ${view.name} ${theme} @notification-dropdown`, async ({ browser }) => {
			const context = await browser.newContext({ viewport: { width: view.w, height: view.h }, deviceScaleFactor: view.dsf });
			const page = await context.newPage();
			await page.setContent(PAGE(STYLES), { waitUntil: "load" });
			await page.evaluate((value) => document.documentElement.setAttribute("data-bs-theme", value), theme);
			const out = {};
			for (const isRead of [false, true]) {
				const items = Object.keys(WORDS).map((sev, i) => item(i + 1, sev, isRead));
				out[isRead] = await page.evaluate((list) => {
					const n = window.JustUpNotifications;
					n.renderPreview(document.getElementById("notif-pop-list"), { items: list }, document, document.getElementById("notif-bell"));
					const pop = document.getElementById("notif-popover").getBoundingClientRect();
					return [...document.querySelectorAll("#notif-pop-list .ju-notif-pop-item")].map((li) => {
						const sev = li.querySelector(".ju-notif-pop-sev");
						const dot = sev.querySelector(".ju-status-dot");
						const r = sev.getBoundingClientRect();
						const btn = li.querySelector(".ju-notif-pop-btn");
						return {
							word: sev.textContent.trim(),
							dotClass: dot.className,
							dotVisible: dot.getBoundingClientRect().width > 0,
							dotHidden: dot.getAttribute("aria-hidden"),
							inside: r.left >= pop.left - 1 && r.right <= pop.right + 1 && r.width > 0,
							border: getComputedStyle(btn).borderLeftColor,
							unreadClass: li.classList.contains("is-unread"),
							dotInUnread: !!li.querySelector(".ju-unread-dot, .ju-status-dot:not(.ju-notif-pop-sev .ju-status-dot)"),
						};
					});
				}, items);
			}
			for (const isRead of [false, true]) {
				Object.keys(WORDS).forEach((sev, i) => {
					const row = out[isRead][i];
					assert.equal(row.word, WORDS[sev], `${sev} word`);
					assert.ok(row.dotClass.includes(`ju-status-dot--${SHAPES[sev]}`), `${sev} dot ${row.dotClass}`);
					assert.ok(row.dotVisible && row.dotHidden === "true" && row.inside, `${sev} dot and word visible inside popover`);
					assert.equal(row.unreadClass, !isRead);
					assert.equal(row.dotInUnread, false, "severity dot is the only status dot");
				});
				// Left marker follows unread only: read rows share one colour whatever the severity.
				if (isRead) assert.equal(new Set(out[true].map((r) => r.border)).size, 1, "read rows carry no severity marker");
			}
			out[false].forEach((r, i) => assert.notEqual(r.border, out[true][i].border, "left marker only for unread"));
			assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 2), true, "no horizontal scroll");
			await context.close();
		});
	}
}
