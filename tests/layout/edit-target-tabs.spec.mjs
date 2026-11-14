//
// tests/layout/edit-target-tabs.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Edit Target in every tab, with every disclosure and check card opened (DESIGN.md DC-SHELL-*, DC-SCROLL-*): the document never scrolls, the footer stays on the last row of the viewport, <main> is the
// only vertical scroller, and nothing runs sideways. The layout audit only sees the first tab in its initial state.
// Measured per scrollbar mode (classic, thin, hidden; hidden is layout-equivalent to overlay), desktop, tablet, phone and
// effective browser zoom (a W x H window at zoom Z is a (W/Z) x (H/Z) CSS viewport at DPR Z; a viewport proxy, not the zoom feature).
import assert from "node:assert/strict";
import { devices } from "@playwright/test";
import { test } from "./fixtures.mjs";
import { effectiveZoom } from "./test-support.mjs";
import { applyScrollbarMode, probeScrollbar, SCROLLBAR_MODES, waitForVisualStability } from "./harness.mjs";

const plain = (w, h, extra = {}) => ({ name: `${w}x${h}`, width: w, height: h, dsf: 1, ...extra });
const zoom = effectiveZoom;
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
		.filter((e) => e.getBoundingClientRect().right > de.clientWidth + 1 && !e.closest(".ju-table-viewport, .ju-tabs, .tag-input, .dropdown-menu"))
		.map(label);
	// Cancel and Save Target (DC-SHELL-4): inside <main>, reachable without scrolling and hit-testable (not under the top bar or the footer).
	// A sticky bar cannot leave its form. When page header, tabs and card header alone fill <main> (extreme zoom), the form starts below
	// the fold; the contract then allows "end of the content", so the actions are measured after scrolling <main> to its end.
	main.scrollTop = 0;
	const form = document.getElementById("target-form");
	const barHeight = document.querySelector("#target-form .ui-form-actions").getBoundingClientRect().height;
	const formStartsInView = form.getBoundingClientRect().top + barHeight <= main.getBoundingClientRect().bottom;
	if (!formStartsInView) main.scrollTop = main.scrollHeight;
	const mainRect = main.getBoundingClientRect();
	const actions = [...document.querySelectorAll("#target-form .ui-form-actions a, #target-form .ui-form-actions button")].map((el) => {
		const r = el.getBoundingClientRect();
		const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
		return {
			text: el.textContent.trim(),
			inMain: r.top >= mainRect.top - 1 && r.bottom <= mainRect.bottom + 1 && r.left >= 0 && r.right <= de.clientWidth + 1,
			hittable: Boolean(hit && (hit === el || el.contains(hit))),
			formStartsInView, roomForActions: main.clientHeight >= barHeight,
			top: Math.round(r.top), bottom: Math.round(r.bottom),
		};
	});
	// DC-SHELL-4 variant 1 (end of the content): where <main> is shorter than the buttons (about 55px of main against 62px buttons at 400% zoom) no button can be
	// fully in view, so the strict check above does not apply. Instead each action is scrolled into view the way a click or Tab does and must show as much of
	// itself as <main> can hold (its full height, or all of <main>), inside <main> (not under the top bar or the footer), and be hit-testable there.
	const actionsAtEnd = [...document.querySelectorAll("#target-form .ui-form-actions a, #target-form .ui-form-actions button")].map((el) => {
		el.scrollIntoView({ block: "nearest", behavior: "instant" });
		const mr = main.getBoundingClientRect();
		const r = el.getBoundingClientRect();
		const top = Math.max(r.top, mr.top);
		const bottom = Math.min(r.bottom, mr.bottom);
		const hit = document.elementFromPoint(r.left + r.width / 2, (top + bottom) / 2);
		return {
			text: el.textContent.trim(),
			// Nothing beyond one pixel of the achievable height may be cut off, and the button stays inside the document sideways.
			inMain: bottom - top >= Math.min(r.height, mr.height) - 1 && r.left >= 0 && r.right <= de.clientWidth + 1,
			hittable: Boolean(hit && (hit === el || el.contains(hit))),
			visible: Math.round(bottom - top), height: Math.round(r.height), mainHeight: Math.round(mr.height),
		};
	});
	main.scrollTop = 0;
	return {
		actions, actionsAtEnd,
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
						assert.equal(m.actions.length, 2, `Cancel and Save Target are missing (${label})`);
						for (const a of m.actions) {
							// At 400% zoom the top bar and footer leave <main> less height than the bar itself, so no sticky bar fits in view there (the CSS then keeps
							// the bar at the end of the content, DC-SHELL-4 variant 1). Those widths are checked at the end of <main> below instead of being skipped.
							if (!a.roomForActions) continue;
							assert.ok(a.inMain, `"${a.text}" is not inside the visible area of main without scrolling (${label})`);
							assert.ok(a.hittable, `"${a.text}" is covered by another element, such as the footer or the top bar (${label})`);
						}
						if (!m.actions.every((a) => a.roomForActions)) {
							assert.deepEqual(m.actionsAtEnd.filter((a) => !a.inMain || !a.hittable), [], `Cancel and Save Target are not reachable at the end of main (${label})`);
						}
						// Focus order: Cancel, then Save Target; Enter in a field submits through the same button.
						await page.locator("#target-form .ui-form-actions a").focus();
						await page.keyboard.press("Tab");
						assert.equal(await page.evaluate(() => document.activeElement.matches('#target-form .ui-form-actions button[type="submit"]')), true, `Tab from Cancel does not reach Save Target (${label})`);
						assert.ok((await page.evaluate(MEASURE)).actions.every((a) => a.inMain || !a.roomForActions), `focusing the actions moved them out of view (${label})`);
						// The last control of the tab is reachable at the end of <main> and the action bar does not cover it; focusing it keeps it clear of the bar.
						const last = await page.evaluate(() => {
							const main = document.querySelector("main");
							const barEl = document.querySelector("#target-form .ui-form-actions");
							const controls = [...document.querySelectorAll(".tab-pane.active input:not([type=hidden]), .tab-pane.active select, .tab-pane.active textarea, .tab-pane.active button, .tab-pane.active a[href], .tab-pane.active summary")]
								.filter((el) => { const r = el.getBoundingClientRect(); return r.width > 2 && r.height > 2 && getComputedStyle(el).visibility !== "hidden" && !el.disabled && !el.closest("[hidden], .visually-hidden, details:not([open]) > :not(summary)"); });
							const control = controls.at(-1);
							if (!control) return null;
							control.focus({ preventScroll: true });
							control.scrollIntoView({ block: "nearest", behavior: "instant" });
							const focused = control.getBoundingClientRect();
							const focusClear = focused.bottom <= barEl.getBoundingClientRect().top + 1 || focused.top >= barEl.getBoundingClientRect().bottom - 1;
							main.scrollTo({ top: main.scrollHeight, behavior: "instant" });
							const r = control.getBoundingClientRect();
							const b = barEl.getBoundingClientRect();
							const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
							const result = { id: control.id || control.tagName, focusClear, focusedBottom: Math.round(focused.bottom), barTop: Math.round(barEl.getBoundingClientRect().top), barHeight: Math.round(b.height), mainBottom: Math.round(main.getBoundingClientRect().bottom), range: main.scrollHeight - main.clientHeight, scrollPadding: getComputedStyle(main).scrollPaddingBottom, room: main.clientHeight >= b.height + focused.height, clearOfBar: r.bottom <= b.top + 1, inMain: r.top >= main.getBoundingClientRect().top - 1, hittable: Boolean(hit && (hit === control || control.contains(hit) || hit.contains(control) || (control.labels && [...control.labels].some((l) => l.contains(hit))))) };
							main.scrollTo({ top: 0, behavior: "instant" });
							return result;
						});
						// At 400% zoom the bar alone is taller than <main>: no control can be in view next to it, so only the focus clearance applies.
						if (last) {
							assert.ok(!last.room || (last.clearOfBar && last.inMain), `the last control of the ${tab} tab is not fully visible above the action bar at the end of main (${label} ${JSON.stringify(last)})`);
							assert.ok(!last.room || last.hittable, `the last control of the ${tab} tab is covered at the end of main (${label} ${JSON.stringify(last)})`);
							assert.ok(last.focusClear, `focusing the last control of the ${tab} tab leaves it under the action bar (${label} ${JSON.stringify(last)})`);
						}
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
