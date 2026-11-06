//
// tests/layout/edit-target-tabs.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Edit Target in every tab, with every disclosure and check card opened (DESIGN.md, "Application viewport and
// scrolling contract"): the document never scrolls, the footer stays on the last row of the viewport, <main> is the
// only vertical scroller, and nothing runs sideways. The layout audit only sees the first tab in its initial state.
// Measured per scrollbar mode (classic, thin, hidden; hidden is layout-equivalent to overlay), desktop, tablet, phone and browser zoom (a W x H window
// at zoom Z is a (W/Z) x (H/Z) CSS viewport at device scale factor Z).
import assert from "node:assert/strict";
import { devices } from "@playwright/test";
import { test } from "./fixtures.mjs";
import { applyScrollbarMode, probeScrollbar, SCROLLBAR_MODES, waitForVisualStability } from "./harness.mjs";

const plain = (w, h, extra = {}) => ({ name: `${w}x${h}`, width: w, height: h, dsf: 1, ...extra });
const zoom = (w, h, z) => ({ name: `${w}x${h}@${z * 100}%`, width: Math.round(w / z), height: Math.round(h / z), dsf: z });
const tablet = (name, label) => ({ name: label, width: devices[name].viewport.width, height: devices[name].viewport.height, dsf: devices[name].deviceScaleFactor, descriptor: devices[name] });
const VIEWPORTS = [
	plain(1280, 720), plain(1024, 768), plain(390, 844), plain(375, 812), plain(320, 640),
	tablet("iPad Pro 11", "tablet-portrait"), tablet("iPad Pro 11 landscape", "tablet-landscape"),
	zoom(1920, 1080, 1.25), zoom(1920, 1080, 1.5), zoom(1280, 720, 2), zoom(1280, 720, 4),
];
const TABS = ["general", "http", "network", "alerts"];

// Containers that scroll by a few pixels only, and every element wider than the viewport.
const MEASURE = () => {
	const de = document.documentElement;
	const main = document.querySelector("main");
	const footer = document.querySelector(".ju-footer, footer");
	const pane = document.querySelector(".tab-pane.active");
	const bar = document.getElementById("target-edit-tabs");
	const label = (el) => `${el.tagName.toLowerCase()}${el.id ? `#${el.id}` : ""}${typeof el.className === "string" && el.className ? `.${el.className.trim().split(/\s+/).slice(0, 2).join(".")}` : ""}`;
	const scrollers = [...document.querySelectorAll("#target-edit-tabs, #target-edit-tabs *, .tab-pane.active, .tab-pane.active *")]
		.filter((el) => /auto|scroll/.test(getComputedStyle(el).overflowY) && el.scrollHeight - el.clientHeight > 1 && !el.matches("textarea, select, .dropdown-menu"))
		.map((el) => `${label(el)} ${el.scrollHeight}>${el.clientHeight}`);
	const sideways = [...pane.querySelectorAll("*")]
		.filter((e) => e.getBoundingClientRect().right > de.clientWidth + 1 && !e.closest(".ju-table-viewport, .nav-tabs, .tag-input, .dropdown-menu"))
		.map(label);
	return {
		barY: bar.scrollHeight - bar.clientHeight,
		barUserScrollsY: /auto|scroll/.test(getComputedStyle(bar).overflowY),
		barX: bar.scrollWidth - bar.clientWidth,
		paneY: pane.scrollHeight - pane.clientHeight,
		docOverflow: de.scrollHeight - de.clientHeight,
		docSideways: de.scrollWidth - de.clientWidth,
		mainSideways: main.scrollWidth - main.clientWidth,
		footerBottom: Math.round(footer.getBoundingClientRect().bottom),
		viewportHeight: innerHeight,
		mainOverflowY: getComputedStyle(main).overflowY,
		mainBottomMeetsFooter: Math.abs(main.getBoundingClientRect().bottom - footer.getBoundingClientRect().top) <= 1,
		paneClipped: /hidden|clip/.test(getComputedStyle(pane).overflowY),
		scrollers,
		sideways,
	};
};

for (const viewport of VIEWPORTS) {
	for (const theme of ["light", "dark"]) {
		test(`edit target keeps the viewport contract in every tab at ${viewport.name} ${theme} @edit-target-tabs`, async ({ browser, browserName, app, adminState }) => {
			for (const mode of SCROLLBAR_MODES[browserName]) {
				const descriptor = { ...(viewport.descriptor || {}) };
				const context = await browser.newContext({
					...descriptor, viewport: { width: viewport.width, height: viewport.height }, deviceScaleFactor: viewport.dsf, storageState: adminState,
				});
				const page = await context.newPage();
				try {
					await applyScrollbarMode(page, mode);
					await page.goto(app.base + "/ui/targets/1/edit", { waitUntil: "load" });
					await page.evaluate((mode) => document.documentElement.setAttribute("data-bs-theme", mode), theme);
					const bars = await probeScrollbar(page);
					if (browserName === "chromium") assert.equal(bars > 0, mode !== "hidden" && !descriptor.isMobile, `${mode}: the emulated scrollbar mode is not in effect (scrollbar layout width ${bars}px)`);
					for (const tab of TABS) {
						await page.click(`#target-${tab}-tab`);
						await page.waitForSelector(`#target-${tab}-pane.show.active`);
						if (tab === "alerts" && viewport.width <= 390) await page.waitForSelector(".alert-recipient-tile", { state: "visible" });
						await waitForVisualStability(page);
						// Everything open: disclosures, collapsed blocks and every check card.
						await page.evaluate(() => {
							document.querySelectorAll(".tab-pane.active details").forEach((d) => { d.open = true; });
							document.querySelectorAll(".tab-pane.active .collapse:not(.show)").forEach((c) => c.classList.add("show"));
							document.querySelectorAll(".tab-pane.active .check-card input[role=switch]").forEach((i) => { if (!i.checked) i.click(); });
						});
						await waitForVisualStability(page);
						const m = await page.evaluate(MEASURE);
						const label = `${browserName}/${mode} (scrollbar ${bars}px) ${tab}: ${JSON.stringify(m)}`;
						// The bar scrolls sideways on narrow screens only; a vertical range the user can reach by wheel or touch is a ghost scroll.
						assert.ok(m.barY <= 0 || !m.barUserScrollsY, `the tab bar can be scrolled vertically, a ghost scroll (${label})`);
						assert.deepEqual(m.scrollers, [], `an element scrolls vertically inside the tab bar or the active tab (${label})`);
						if (viewport.width >= 768) assert.ok(m.barX <= 0, `the tab bar scrolls sideways (${label})`);
						assert.ok(m.docOverflow <= 1, `the document scrolls vertically (${label})`);
						assert.ok(m.docSideways <= 1, `the document scrolls sideways (${label})`);
						assert.ok(m.mainSideways <= 1, `main scrolls sideways (${label})`);
						assert.equal(m.footerBottom, m.viewportHeight, `the footer is not on the last row of the viewport (${label})`);
						assert.ok(["auto", "scroll"].includes(m.mainOverflowY), `main does not scroll (${label})`);
						assert.ok(m.mainBottomMeetsFooter, `a gap or overlap between main and footer (${label})`);
						assert.equal(m.paneClipped, false, `the tab pane clips its content (${label})`);
						assert.deepEqual(m.sideways, [], `content runs past the right edge (${label})`);
						if (tab === "alerts" && viewport.width <= 390) {
							const card = await page.evaluate(() => {
								const grid = document.querySelector(".alert-recipients-grid").getBoundingClientRect();
								const tile = document.querySelector(".alert-recipient-tile").getBoundingClientRect();
								return { gridWidth: grid.width, tileWidth: tile.width };
							});
							assert.ok(Math.abs(card.tileWidth - card.gridWidth) <= 1, `recipient card does not use the narrow grid width (${JSON.stringify(card)})`);
						}
					}
				} finally {
					await context.close();
				}
			}
		});
	}
}
