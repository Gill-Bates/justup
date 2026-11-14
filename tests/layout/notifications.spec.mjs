//
// tests/layout/notifications.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { test as base } from "@playwright/test";
import { seed, startApp } from "./harness.mjs";
import {
	auditNotificationBehaviour,
	auditNotificationRows,
	NOTIFICATION_VIEWPORTS,
	seedNotifications,
} from "./notifications.mjs";

const test = base.extend({
	notificationApp: [async ({ browser }, use) => {
		const app = await startApp();
		try {
			const adminState = await seed(browser, app);
			seedNotifications(app);
			await use({ app, adminState });
		} finally {
			await app.stop();
		}
	}, { scope: "worker", timeout: 90_000 }],
	storageState: async ({ notificationApp }, use) => {
		await use(notificationApp.adminState);
	},
});

for (const viewport of NOTIFICATION_VIEWPORTS) {
	test.describe(`notification rows at ${viewport.name}`, () => {
		test.use({ viewport: { width: viewport.w, height: viewport.h }, deviceScaleFactor: viewport.dsf });
		test(`layout and interaction @notifications`, async ({ page, browserName, notificationApp }) => {
			const findings = await auditNotificationRows(page, notificationApp.app, browserName, viewport);
			assert.deepEqual(findings, [], `${browserName}/${viewport.name} notification findings`);
		});
	});
}

test("notification actions preserve links, read state and keyboard deletion @notifications", async ({ page, browserName, notificationApp }) => {
	await auditNotificationBehaviour(page, notificationApp.app, browserName);
});
test("a failed delete keeps focus on the delete button and leaves it operable @notifications", async ({ page, notificationApp }) => {
	await page.goto(`${notificationApp.app.base}/ui/notifications`, { waitUntil: "load" });
	await page.route("**/dismiss", (route) => route.fulfill({ status: 500, body: "{}" }));
	const rows = page.locator("#np-list > .ju-notif-item");
	const row = rows.nth(2);
	await row.locator(":scope > .ju-notif-swipe-content").focus();
	await page.keyboard.press("Tab");
	const button = row.locator(".ju-notif-dismiss-button");
	assert.ok(await button.evaluate((el) => el === document.activeElement), "Tab reaches the delete button");
	const failed = page.waitForResponse((response) => response.url().endsWith("/dismiss"));
	await page.keyboard.press("Enter");
	assert.equal((await failed).status(), 500);
	await page.waitForFunction(() => document.querySelector(".ju-notif-dismiss-button[aria-busy='true']") === null);
	assert.ok(await button.evaluate((el) => el === document.activeElement), "focus stays on the delete button after the failure");
	assert.equal(await button.getAttribute("aria-busy"), null);
});

// N42 / DC-THEME-2: in forced colors the author colours and box-shadows are replaced by system colours, so the unread marker
// (ringed dot, bold title) must survive without them. The dot is decorative; the visually hidden "Unread: " stays the text cue.
test("forced colors keep the unread marker visible on the notifications page @notifications", async ({ page, notificationApp }) => {
	await page.emulateMedia({ forcedColors: "active" });
	await page.goto(`${notificationApp.app.base}/ui/notifications`, { waitUntil: "load" });
	assert.equal(await page.evaluate(() => window.matchMedia("(forced-colors: active)").matches), true, "forced colors emulation is active");
	const rows = await page.evaluate(() => {
		const system = (name) => { const probe = document.createElement("i"); probe.style.color = name; document.body.append(probe); const colour = getComputedStyle(probe).color; probe.remove(); return colour; };
		const items = [...document.querySelectorAll("#np-list > .ju-notif-item")].map((item) => {
			const dot = item.querySelector(".ju-unread-dot");
			const dotStyle = dot ? getComputedStyle(dot) : null;
			const title = getComputedStyle(item.querySelector(".ju-notif-title-text, .ju-notif-title"));
			return {
				unread: item.classList.contains("is-unread"), weight: Number(title.fontWeight), hasDot: Boolean(dot),
				itemBg: getComputedStyle(item).backgroundColor, dotBg: dotStyle?.backgroundColor ?? null,
				dotRing: dotStyle ? dotStyle.outlineStyle !== "none" && parseFloat(dotStyle.outlineWidth) >= 1 : false,
				hiddenText: dot?.nextElementSibling?.matches(".visually-hidden") ? dot.nextElementSibling.textContent.trim() : "",
			};
		});
		return { items, highlight: system("Highlight") };
	});
	const unread = rows.items.filter((row) => row.unread);
	const read = rows.items.filter((row) => !row.unread);
	assert.ok(unread.length > 0 && read.length > 0, "the seeded page shows unread and read rows");
	for (const row of unread) {
		assert.ok(row.hasDot, "an unread row has its marker dot");
		assert.notEqual(row.dotBg, row.itemBg, "the unread dot disappears into the row background in forced colors");
		assert.equal(row.dotBg, rows.highlight, "the unread dot uses the system Highlight colour");
		assert.ok(row.dotRing, "the unread dot keeps a ring that does not depend on box-shadow");
		assert.equal(row.hiddenText, "Unread:", "the unread state also has a text cue");
	}
	const lightest = Math.min(...unread.map((row) => row.weight));
	const heaviest = Math.max(...read.map((row) => row.weight));
	assert.ok(lightest > heaviest, `unread titles stay heavier than read titles (${lightest} vs ${heaviest})`);
	for (const row of read) assert.equal(row.hasDot, false, "a read row has no marker dot");
});

// DC-STATE-4 on the S4 surface: the hollow ring is "information, no severity". Every Info item is a ring, and no error, warning or
// success item is one; the ring never carries the words of a state (Unknown, Down) on this surface.
test("notification severity: the hollow ring is the Info marker and nothing else @notifications", async ({ page, notificationApp }) => {
	await page.goto(`${notificationApp.app.base}/ui/notifications`, { waitUntil: "load" });
	const items = await page.evaluate(() => [...document.querySelectorAll("#np-list .ju-status-item")].map((item) => ({
		word: item.textContent.replace(/\s+/g, " ").trim(),
		hollow: getComputedStyle(item.querySelector(".ju-status-dot")).backgroundColor === "rgba(0, 0, 0, 0)",
	})));
	assert.deepEqual([...new Set(items.map((item) => item.word))].sort(), ["Error", "Info", "Success", "Warning"], "the seeded page shows all four severities");
	for (const item of items) assert.equal(item.hollow, item.word === "Info", `${item.word}: ring ${item.hollow}`);
});
// OR-1 / DC-NOTIF-4 / DC-FOCUS-6: a keyboard-focused notification row is the V3 (inset) variant, because the row clips an outer ring.
for (const theme of ["light", "dark"]) {
	test(`a keyboard-focused notification row draws the V3 inset ring (${theme}) @notifications @focus`, async ({ page, notificationApp }) => {
		await page.setViewportSize({ width: 1280, height: 900 });
		await page.goto(`${notificationApp.app.base}/ui/notifications`, { waitUntil: "load" });
		await page.evaluate((mode) => document.documentElement.setAttribute("data-bs-theme", mode), theme);
		await page.waitForSelector("#np-list > .ju-notif-item");
		await page.keyboard.press("Tab"); // keyboard modality, so :focus-visible applies to the focus() call
		const row = page.locator("#np-list > .ju-notif-item > .ju-notif-swipe-content.is-actionable").first();
		await row.focus();
		const state = await row.evaluate((el) => {
			const probe = document.createElement("span");
			probe.style.cssText = "outline: var(--ju-focus-outline-width) solid var(--ju-focus-ring); position: absolute; visibility: hidden;";
			document.body.append(probe);
			const want = getComputedStyle(probe);
			const tokens = { width: want.outlineWidth, colour: want.outlineColor };
			probe.remove();
			const cs = getComputedStyle(el);
			return { tokens, focusVisible: el.matches(":focus-visible"), style: cs.outlineStyle, width: cs.outlineWidth, colour: cs.outlineColor, offset: cs.outlineOffset, boxShadow: cs.boxShadow };
		});
		assert.equal(state.focusVisible, true, "the row is focus-visible");
		assert.equal(state.style, "solid");
		assert.equal(state.width, state.tokens.width, "the ring reads --ju-focus-outline-width");
		assert.equal(state.colour, state.tokens.colour, "the ring reads --ju-focus-ring");
		assert.equal(parseFloat(state.offset), -parseFloat(state.tokens.width), "V3: the offset is the negative ring width");
		assert.equal(state.boxShadow, "none", "no box-shadow ring");
	});
}
