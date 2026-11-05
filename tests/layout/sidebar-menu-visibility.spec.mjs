//
// tests/layout/sidebar-menu-visibility.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { test } from "./fixtures.mjs";

const MENU = [
	{ href: "/", label: "Dashboard" },
	{ href: "/ui/targets", label: "Targets" },
	{ href: "/ui/notifications", label: "Notifications" },
	{ href: "/ui/groups", label: "Groups" },
	{ href: "/ui/settings", label: "Settings" },
	{ href: "/ui/about", label: "About" },
];

async function assertMenuVisible(page, { labels, label }) {
	const sidebar = page.locator("#app-sidebar");
	const nav = sidebar.locator(".ju-nav");
	for (const item of MENU) {
		const link = sidebar.locator(`.ju-nav a[href="${item.href}"]`);
		assert.equal(await link.count(), 1, `${label}: expected one ${item.label} menu link`);
		assert.equal(await link.isVisible(), true, `${label}: ${item.label} link is hidden`);
		const state = await link.evaluate((el) => {
			const box = el.getBoundingClientRect();
			const navBox = el.closest(".ju-nav").getBoundingClientRect();
			const text = el.querySelector(".ju-nav-text");
			const textBox = text.getBoundingClientRect();
			const style = getComputedStyle(el);
			return {
				left: box.left, right: box.right, top: box.top, bottom: box.bottom,
				navLeft: navBox.left, navRight: navBox.right, navTop: navBox.top, navBottom: navBox.bottom,
				labelVisible: getComputedStyle(text).display !== "none" && textBox.width > 0,
				iconVisible: !!el.querySelector(".material-icons")?.getBoundingClientRect().width,
				title: el.getAttribute("title"), display: style.display, visibility: style.visibility,
			};
		});
		assert.notEqual(state.display, "none", `${label}: ${item.label} has display:none`);
		assert.equal(state.visibility, "visible", `${label}: ${item.label} is not visible`);
		assert.ok(state.iconVisible, `${label}: ${item.label} icon is missing`);
		assert.ok(state.right <= state.navRight + 1 && state.left >= state.navLeft - 1, `${label}: ${item.label} is clipped horizontally: ${JSON.stringify(state)}`);
		assert.ok(state.top >= state.navTop - 1 && state.bottom <= state.navBottom + 1, `${label}: ${item.label} is clipped vertically: ${JSON.stringify(state)}`);
		if (labels) assert.ok(state.labelVisible, `${label}: ${item.label} text is not visible`);
		else assert.ok(state.title, `${label}: collapsed ${item.label} icon has no tooltip`);
	}
	assert.equal(await nav.locator(".ju-nav-group-toggle").isVisible(), true, `${label}: Manage group is hidden`);
}

test("all application menu entries remain visible in desktop, rail, and mobile navigation @sidebar", async ({ browser, app, adminState }) => {
	const context = await browser.newContext({ viewport: { width: 1280, height: 720 }, storageState: adminState });
	const page = await context.newPage();
	try {
		await page.goto(`${app.base}/ui/about`, { waitUntil: "load" });
		await page.evaluate(() => document.fonts.ready);
		await assertMenuVisible(page, { labels: true, label: "desktop expanded" });
		assert.equal(await page.locator('#app-sidebar .ju-nav a[href="/ui/about"]').getAttribute("aria-current"), "page");

		await page.locator("#sidebar-collapse").click();
		await page.waitForFunction(() => document.documentElement.classList.contains("ju-nav-sm"));
		await assertMenuVisible(page, { labels: false, label: "desktop collapsed" });

		await page.setViewportSize({ width: 390, height: 844 });
		await page.evaluate(() => document.documentElement.classList.remove("ju-nav-sm"));
		const toggle = page.getByRole("button", { name: "Toggle navigation" });
		await toggle.click();
		await page.waitForFunction(() => document.documentElement.classList.contains("ju-nav-open"));
		await assertMenuVisible(page, { labels: true, label: "mobile drawer" });
	} finally {
		await context.close();
	}
});
