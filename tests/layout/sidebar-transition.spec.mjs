//
// tests/layout/sidebar-transition.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { expect, test } from "@playwright/test";
import { test as appTest } from "./fixtures.mjs";
import { accentContrast, assertExpanded, focusRing, matrix, mobile, transition } from "./sidebar-transition.mjs";

test("sidebar transition preserves alignment throughout animation @sidebar", async ({ page }) => {
	const { initial, frames } = await transition(page);
	assertExpanded(initial, "initial");
	assert.equal(frames.length, 22, "both transitions were sampled at fixed progress points");
	for (const [index, frame] of frames.entries()) {
		assert.ok(Math.abs(frame.sidebarW - frame.contentMargin) <= 1.5, `content margin diverged from sidebar width at frame ${index}`);
		assert.ok(Math.abs(frame.contentLeft - frame.sidebarRight) <= 1.5, `content overlaps or detaches from the sidebar at frame ${index}`);
		assert.ok(Math.abs(frame.toggle.iconLeft - initial.toggle.iconLeft) <= 1 && Math.abs(frame.toggle.top - initial.toggle.top) <= 1,
			`toggle icon moved at frame ${index}: ${initial.toggle.iconLeft} -> ${frame.toggle.iconLeft}, top ${initial.toggle.top} -> ${frame.toggle.top}`);
	}
});

test("sidebar desktop layout remains stable across size, theme and rail states @sidebar", async ({ page }) => {
	await matrix(page);
});

test("sidebar keyboard focus and group controls remain operable @sidebar", async ({ page }) => {
	await focusRing(page);
});

test("sidebar accent and link colours keep their contrast in both themes @sidebar", async ({ page }) => {
	await accentContrast(page);
});

test("mobile navigation drawer remains reachable and correctly labelled @sidebar", async ({ page }) => {
	await mobile(page);
});

// DC-SCROLL-6 / DC-NAV-3 on the real shell: while the mobile drawer is open, <main> must not scroll by wheel, touch or keyboard,
// and closing it leaves the position unchanged. A control gesture proves the same gestures scroll <main> with the drawer closed.
appTest("open mobile drawer locks the main scroll region @sidebar", async ({ browser, browserName, app, adminState }) => {
	const [width, height] = [390, 844];
	const context = await browser.newContext({ viewport: { width, height }, hasTouch: true, storageState: adminState });
	try {
		const page = await context.newPage();
		await page.goto(`${app.base}/ui/groups`, { waitUntil: "load" });
		const mainTop = () => page.evaluate(() => document.getElementById("main-content").scrollTop);
		const frames = (count) => page.evaluate((n) => new Promise((resolve) => {
			const step = (left) => (left ? requestAnimationFrame(() => step(left - 1)) : resolve());
			step(n);
		}), count);
		await page.evaluate(() => {
			const main = document.getElementById("main-content");
			const filler = document.createElement("div");
			filler.style.height = "4000px";
			main.append(filler);
			main.scrollTop = 300;
		});
		await page.mouse.move(width / 2, height / 2);
		await page.mouse.wheel(0, 200);
		await expect.poll(mainTop, { message: "control: wheel scrolls <main> with the drawer closed" }).toBeGreaterThan(300);
		await page.evaluate(() => { document.getElementById("main-content").scrollTop = 300; });
		await page.click("#sidebar-toggle");
		await page.waitForFunction(() => document.documentElement.classList.contains("ju-nav-open"));
		await page.waitForFunction(() => {
			const sidebar = document.getElementById("app-sidebar");
			return sidebar && sidebar.getAnimations().every((animation) => animation.playState === "finished");
		});
		const drawer = await page.evaluate(() => document.getElementById("app-sidebar").getBoundingClientRect().right);
		assert.ok(drawer > 100 && drawer < width - 40, `the drawer leaves room for the backdrop (right edge ${drawer})`);
		const backdropX = (drawer + width) / 2;
		for (const x of [drawer / 2, backdropX]) {
			await page.mouse.move(x, height / 2);
			await page.mouse.wheel(0, 600);
		}
		await page.focus("#app-sidebar .ju-nav-link");
		for (const key of ["PageDown", "Space", "ArrowDown", "End"]) await page.keyboard.press(key);
		if (browserName === "chromium") {
			const cdp = await context.newCDPSession(page);
			for (const x of [drawer / 2, backdropX]) {
				await cdp.send("Input.synthesizeScrollGesture", { x, y: height / 2, yDistance: -400, gestureSourceType: "touch", speed: 800 });
			}
		}
		await frames(6);
		assert.equal(await mainTop(), 300, "<main> scrolled behind the open drawer");
		await page.keyboard.press("Escape");
		await page.waitForFunction(() => !document.documentElement.classList.contains("ju-nav-open"));
		assert.equal(await mainTop(), 300, "closing the drawer changed the scroll position of <main>");
	} finally {
		await context.close();
	}
});
// DC-NAV-3 on the real shell: the mobile drawer is a disclosure. Escape closes it and returns focus to the toggle, tabbing out of it
// closes it without trapping focus, and leaving the mobile layout removes its state.
const openDrawer = async (page) => {
	await page.click("#sidebar-toggle");
	await page.waitForFunction(() => document.documentElement.classList.contains("ju-nav-open"));
};
const drawerClosed = (page) => page.waitForFunction(() => !document.documentElement.classList.contains("ju-nav-open"));
appTest("mobile drawer: Escape closes it and returns focus to the toggle @sidebar", async ({ browser, app, adminState }) => {
	const context = await browser.newContext({ viewport: { width: 390, height: 844 }, storageState: adminState });
	try {
		const page = await context.newPage();
		await page.goto(`${app.base}/ui/groups`, { waitUntil: "load" });
		await openDrawer(page);
		assert.equal(await page.getAttribute("#sidebar-toggle", "aria-expanded"), "true");
		assert.equal(await page.evaluate(() => document.getElementById("app-sidebar").contains(document.activeElement)), true, "opening moves focus into the drawer");
		await page.keyboard.press("Escape");
		await drawerClosed(page);
		assert.equal(await page.getAttribute("#sidebar-toggle", "aria-expanded"), "false");
		assert.equal(await page.evaluate(() => document.activeElement?.id), "sidebar-toggle", "closing with Escape returns focus to the toggle");
	} finally {
		await context.close();
	}
});
appTest("mobile drawer: tabbing out of it closes it and focus is not trapped @sidebar", async ({ browser, app, adminState }) => {
	const context = await browser.newContext({ viewport: { width: 390, height: 844 }, storageState: adminState });
	try {
		const page = await context.newPage();
		await page.goto(`${app.base}/ui/groups`, { waitUntil: "load" });
		await openDrawer(page);
		// Walk forward from the first drawer control until focus leaves the drawer (a trap would never leave it).
		let left = false;
		for (let press = 0; press < 40 && !left; press += 1) {
			await page.keyboard.press("Tab");
			left = await page.evaluate(() => !document.getElementById("app-sidebar").contains(document.activeElement));
		}
		assert.equal(left, true, "Tab leaves the drawer: it has no focus trap");
		await drawerClosed(page);
		assert.equal(await page.getAttribute("#sidebar-toggle", "aria-expanded"), "false", "tabbing out collapsed the disclosure");
		assert.equal(await page.evaluate(() => document.getElementById("main-content").contains(document.activeElement)), true, "focus continues in the page content");
	} finally {
		await context.close();
	}
});
appTest("mobile drawer: leaving the mobile layout removes its open state @sidebar", async ({ browser, app, adminState }) => {
	const context = await browser.newContext({ viewport: { width: 1280, height: 720 }, storageState: adminState });
	try {
		const page = await context.newPage();
		await page.goto(`${app.base}/ui/groups`, { waitUntil: "load" });
		// Collapse the desktop rail first, so a leftover "open" attribute cannot be mistaken for the desktop state.
		await page.click("#sidebar-collapse");
		await page.waitForFunction(() => document.documentElement.classList.contains("ju-nav-sm"));
		await page.setViewportSize({ width: 390, height: 844 });
		await openDrawer(page);
		assert.equal(await page.getAttribute("#sidebar-toggle", "aria-expanded"), "true");
		await page.setViewportSize({ width: 1280, height: 720 });
		await drawerClosed(page);
		assert.equal(await page.getAttribute("#sidebar-toggle", "aria-expanded"), "false", "the toggle mirrors the collapsed desktop rail, not the drawer that was open");
		// Back on the phone layout the drawer starts closed (nothing was left behind).
		await page.setViewportSize({ width: 390, height: 844 });
		assert.equal(await page.evaluate(() => document.documentElement.classList.contains("ju-nav-open")), false, "the drawer stays closed when the phone layout returns");
		assert.equal(await page.getAttribute("#sidebar-toggle", "aria-expanded"), "false");
	} finally {
		await context.close();
	}
});
