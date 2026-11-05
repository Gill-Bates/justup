//
// tests/layout/edit-target-tabs.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Edit Target in every tab, with every disclosure and check card opened (DESIGN.md, "Application viewport and
// scrolling contract"): the document never scrolls, the footer stays on the last row of the viewport, <main> is the
// only vertical scroller, and nothing runs sideways. The layout audit only sees the first tab in its initial state.
import assert from "node:assert/strict";
import { test } from "./fixtures.mjs";

const VIEWPORTS = [
	{ name: "1280x720", width: 1280, height: 720 },
	{ name: "1024x768", width: 1024, height: 768 },
	{ name: "390x844", width: 390, height: 844 },
	{ name: "320x640", width: 320, height: 640 },
];
const TABS = ["general", "http", "network", "alerts"];

for (const viewport of VIEWPORTS) {
	for (const theme of ["light", "dark"]) {
		test(`edit target keeps the viewport contract in every tab at ${viewport.name} ${theme} @edit-target-tabs`, async ({ browser, app, adminState }) => {
			const context = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height }, storageState: adminState });
			const page = await context.newPage();
			try {
				await page.goto(app.base + "/ui/targets/1/edit", { waitUntil: "load" });
				await page.evaluate((mode) => document.documentElement.setAttribute("data-bs-theme", mode), theme);
				for (const tab of TABS) {
					await page.click(`#target-${tab}-tab`);
					await page.waitForTimeout(350);
					// Everything open: disclosures, collapsed blocks and every check card.
					await page.evaluate(() => {
						document.querySelectorAll(".tab-pane.active details").forEach((d) => { d.open = true; });
						document.querySelectorAll(".tab-pane.active .collapse:not(.show)").forEach((c) => c.classList.add("show"));
						document.querySelectorAll(".tab-pane.active .check-card input[role=switch]").forEach((i) => { if (!i.checked) i.click(); });
					});
					await page.waitForTimeout(300);
					const m = await page.evaluate(() => {
						const de = document.documentElement;
						const main = document.querySelector("main");
						const footer = document.querySelector(".ju-footer, footer");
						const pane = document.querySelector(".tab-pane.active");
						const sideways = [...pane.querySelectorAll("*")].filter((e) => e.getBoundingClientRect().right > de.clientWidth + 1 && !e.closest(".ju-table-viewport, .nav-tabs, .tag-input, .dropdown-menu")).length;
						const bar = document.getElementById("target-edit-tabs");
						return {
							// The bar scrolls sideways on narrow screens only; a vertical range the user can reach by wheel or touch is a ghost scroll.
							barY: bar.scrollHeight - bar.clientHeight,
							barUserScrollsY: /auto|scroll/.test(getComputedStyle(bar).overflowY),
							barX: bar.scrollWidth - bar.clientWidth,
							docOverflow: de.scrollHeight - de.clientHeight,
							docSideways: de.scrollWidth - de.clientWidth,
							footerBottom: Math.round(footer.getBoundingClientRect().bottom),
							viewportHeight: innerHeight,
							mainOverflowY: getComputedStyle(main).overflowY,
							mainBottomMeetsFooter: Math.abs(main.getBoundingClientRect().bottom - footer.getBoundingClientRect().top) <= 1,
							paneClipped: /hidden|clip/.test(getComputedStyle(pane).overflowY),
							sideways,
						};
					});
					const label = `${tab}: ${JSON.stringify(m)}`;
					assert.ok(m.barY <= 0 || !m.barUserScrollsY, `the tab bar can be scrolled vertically, a ghost scroll (${label})`);
					if (viewport.width >= 768) assert.ok(m.barX <= 0, `the tab bar scrolls sideways (${label})`);
					assert.ok(m.docOverflow <= 1, `the document scrolls vertically (${label})`);
					assert.ok(m.docSideways <= 1, `the document scrolls sideways (${label})`);
					assert.equal(m.footerBottom, m.viewportHeight, `the footer is not on the last row of the viewport (${label})`);
					assert.ok(["auto", "scroll"].includes(m.mainOverflowY), `main does not scroll (${label})`);
					assert.ok(m.mainBottomMeetsFooter, `a gap or overlap between main and footer (${label})`);
					assert.equal(m.paneClipped, false, `the tab pane clips its content (${label})`);
					assert.equal(m.sideways, 0, `content runs past the right edge (${label})`);
				}
			} finally {
				await context.close();
			}
		});
	}
}
