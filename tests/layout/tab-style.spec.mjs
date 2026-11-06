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
					await page.waitForFunction(() => document.getAnimations().length === 0);
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
				const { overflowMarked, fadeEnd, ...rest } = look;
				const { overflowMarked: _o, fadeEnd: _f, ...ref } = reference;
				assert.deepEqual(rest, ref, `${name} differs from the Notifications tab look`);
			}
		});
	}
}
