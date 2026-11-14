//
// tests/layout/tab-style.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Every tab strip is the one shared component (DESIGN.md, "Tabs"): the computed look of the strip, an inactive tab and
// the active tab is identical on all pages, in both themes, wide and narrow. Notifications is the reference.
import assert from "node:assert/strict";
import { test } from "./fixtures.mjs";

const PAGES = [
	{ name: "notifications", path: "/ui/notifications", strip: ".ju-tabs" },
	{ name: "edit-target", path: "/ui/targets/1/edit", strip: "#target-edit-tabs" },
	{ name: "target-detail", path: "/ui/targets/1", strip: "#target-detail-tabs" },
	{ name: "settings", path: "/ui/settings", strip: "#settingsTabs" },
];

const LOOK = (selector) => {
	const strip = document.querySelector(selector);
	const pick = (el, props) => Object.fromEntries(props.map((p) => [p, getComputedStyle(el)[p]]));
	const link = (el) => pick(el, ["color", "backgroundColor", "fontWeight", "borderRadius", "paddingLeft", "minHeight", "borderTopWidth"]);
	return {
		hasShared: strip.classList.contains("ju-tabs"),
		// The overflow hint (edge fade) is present exactly when tabs are out of view.
		overflowMarked: (strip.scrollWidth - strip.clientWidth > 1) === strip.classList.contains("has-overflow"),
		fadeEnd: strip.classList.contains("has-overflow") ? !strip.classList.contains("is-at-end") : null,
		strip: pick(strip, ["flexWrap", "overflowX", "overflowY", "columnGap", "scrollbarColor", "scrollbarWidth"]),
		inactive: link(strip.querySelector(".nav-link:not(.active)")),
		active: link(strip.querySelector(".nav-link.active")),
		primary: getComputedStyle(document.documentElement).getPropertyValue("--bs-primary").trim(),
	};
};

for (const theme of ["light", "dark"]) {
	for (const width of [1280, 390]) {
		test(`all tab strips share one look at ${width}px ${theme} @tab-style`, async ({ browser, app, adminState }) => {
			const context = await browser.newContext({ viewport: { width, height: 844 }, storageState: adminState });
			const looks = {};
			try {
				for (const page_ of PAGES) {
					const page = await context.newPage();
					await page.goto(app.base + page_.path, { waitUntil: "load" });
					await page.evaluate((mode) => document.documentElement.setAttribute("data-bs-theme", mode), theme);
					await page.waitForSelector(page_.strip);
					// Bootstrap tabs transition colour; measure only after the theme switch has settled.
					await page.waitForFunction((sel) => document.querySelector(sel).getAnimations({ subtree: true }).length === 0, page_.strip, { timeout: 10000 });
					looks[page_.name] = await page.evaluate(LOOK, page_.strip);
					await page.close();
				}
			} finally {
				await context.close();
			}
			const reference = looks.notifications;
			assert.ok(reference.hasShared, "the reference strip must be the shared component");
			assert.notEqual(reference.active.backgroundColor, "rgba(0, 0, 0, 0)", "the active tab must be filled, not colour-only text");
			assert.equal(reference.strip.flexWrap, "nowrap");
			for (const [name, look] of Object.entries(looks)) {
				assert.ok(look.hasShared, `${name}: strip is not .ju-tabs`);
				assert.ok(look.overflowMarked, `${name}: has-overflow does not match the real overflow`);
				if (look.fadeEnd !== null) assert.equal(look.fadeEnd, true, `${name}: end fade missing although tabs overflow at the start position`);
				const { overflowMarked, fadeEnd, ...rest } = look;
				const { overflowMarked: _o, fadeEnd: _f, ...ref } = reference;
				assert.deepEqual(rest, ref, `${name} differs from the Notifications tab look`);
			}
		});
	}
}
// Same look, different semantics (DC-TAB-6 to DC-TAB-9): real tabs use roving tabindex and arrow keys, link strips stay plain links.
test("real tabs: roving tabindex, arrow keys and native disabled @tab-style", async ({ browser, app, adminState }) => {
	const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, storageState: adminState });
	try {
		const page = await context.newPage();
		await page.goto(`${app.base}/ui/settings`, { waitUntil: "load" });
		await page.waitForSelector("#settingsTabs");
		const tabs = () => page.evaluate(() => [...document.querySelectorAll("#settingsTabs [role=tab]")].map((t) => [t.id, t.tabIndex, t.getAttribute("aria-selected")]));
		const rows = await tabs();
		assert.deepEqual(rows.filter(([, index]) => index === 0).map(([id]) => id), [rows.find(([, , selected]) => selected === "true")[0]], "only the active tab is in the Tab sequence");
		await page.locator("#general-tab").focus();
		await page.keyboard.press("ArrowRight");
		assert.equal(await page.evaluate(() => document.activeElement.id), "network-tab");
		assert.equal(await page.getAttribute("#network-tab", "aria-selected"), "true", "arrow keys activate (automatic activation)");
		await page.keyboard.press("End");
		assert.equal(await page.evaluate(() => document.activeElement.id), "spooler-tab");
		await page.keyboard.press("Home");
		assert.equal(await page.evaluate(() => document.activeElement.id), "general-tab");
		// A native disabled tab is skipped by the arrow keys and cannot be activated.
		await page.evaluate(() => { document.getElementById("network-tab").disabled = true; });
		await page.keyboard.press("ArrowRight");
		assert.equal(await page.evaluate(() => document.activeElement.id), "storage-tab");
		assert.equal(await page.evaluate(() => document.getElementById("network-tab").hasAttribute("aria-disabled")), false, "aria-disabled is not used on tabs");
		await page.locator("#network-tab").click({ force: true });
		assert.equal(await page.getAttribute("#network-tab", "aria-selected"), "false", "a disabled tab does not activate by pointer");
	} finally {
		await context.close();
	}
});
test("link strip: a nav landmark, all links in the Tab sequence, a disabled link is inert @tab-style", async ({ browser, app, adminState }) => {
	const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, storageState: adminState });
	try {
		const page = await context.newPage();
		await page.goto(`${app.base}/ui/notifications`, { waitUntil: "load" });
		await page.waitForSelector(".ju-tabs");
		assert.equal(await page.evaluate(() => document.querySelector(".ju-tabs").parentElement.matches("nav[aria-label]")), true);
		assert.equal(await page.evaluate(() => document.querySelectorAll(".ju-tabs [role=tab], .ju-tabs[role=tablist]").length), 0, "no tab roles on navigation links");
		const hrefs = await page.evaluate(() => [...document.querySelectorAll(".ju-tabs a.nav-link")].map((a) => a.tabIndex));
		assert.ok(hrefs.length >= 2 && hrefs.every((index) => index === 0), "every link is in the Tab sequence");
		// Disabled variant (no consumer yet): no href, aria-disabled, tabindex -1.
		const url = page.url();
		await page.evaluate(() => {
			const link = document.querySelectorAll(".ju-tabs a.nav-link")[1];
			link.removeAttribute("href");
			link.classList.add("disabled");
			link.setAttribute("aria-disabled", "true");
			link.setAttribute("tabindex", "-1");
		});
		await page.locator(".ju-tabs a.nav-link").first().focus();
		await page.keyboard.press("Tab");
		const focus = await page.evaluate(() => document.activeElement.closest(".ju-tabs") ? document.activeElement.textContent.trim() : "outside");
		const disabledText = await page.locator(".ju-tabs a.nav-link").nth(1).innerText();
		assert.notEqual(focus, disabledText.trim(), "Tab skips the disabled link");
		await page.locator(".ju-tabs a.nav-link").nth(1).click({ force: true });
		assert.equal(page.url(), url, "a disabled link does not navigate");
	} finally {
		await context.close();
	}
});
