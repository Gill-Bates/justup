//
// tests/layout/load-stability.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// Load stability of the target detail page (DESIGN.md, "Load stability"): the page must not flicker
// while it loads or refreshes. Theme and sidebar state are right in the first frame, no shell
// element is dimmed or animated, the page structure is never replaced, and nothing that is visible
// at first paint moves afterwards.
import assert from "node:assert/strict";
import { test } from "./fixtures.mjs";

const TARGET_PATH = "/ui/targets/4";
const VIEWPORTS = [{ w: 1280, h: 720 }, { w: 390, h: 844 }];
const THEMES = ["light", "dark"];
const WINDOW_MS = 3500;
// Sum of all layout shifts in a cold load. Measured after the fix: 0 on the shell and header; the
// budget leaves room for sub-pixel rounding of chart and map content only.
const CLS_BUDGET = 0.01;
const LANDMARK_TOLERANCE_PX = 1;
const SHELL_SELECTOR = ".ju-shell, .ju-content, .ju-topbar, .ju-sidebar, main, .target-detail-page, .target-detail-heading";

// Runs before any page script: records frames, attribute changes, dimming and node replacement.
function recorder(sidebarCollapsed) {
	const record = { frames: [], themeWrites: [], dimmed: [], transitions: [], removed: [], shifts: 0 };
	window.__stability = record;
	try {
		if (sidebarCollapsed) localStorage.setItem("ju-sidebar", "collapsed");
	} catch { /* storage blocked */ }
	try {
		new PerformanceObserver((list) => {
			for (const entry of list.getEntries()) if (!entry.hadRecentInput) record.shifts += entry.value;
		}).observe({ type: "layout-shift", buffered: true });
	} catch { /* WebKit has no layout-shift entries; landmark stability covers it */ }
	const rectOf = (selector) => {
		const element = document.querySelector(selector);
		if (!element) return null;
		const box = element.getBoundingClientRect();
		return [box.x, box.y, box.width, box.height].map(Math.round);
	};
	const landmarks = { nav: ".ju-nav-text", topbar: ".ju-topbar-actions", title: ".target-detail-heading h1", actions: ".target-detail-actions", tabs: "#target-detail-tabs", kpi: ".target-kpi-row" };
	const frame = () => {
		const root = document.documentElement;
		if (document.body) {
			const dimmed = [document.body, ...document.querySelectorAll(".ju-shell, .ju-content, main")].filter((el) => Number(getComputedStyle(el).opacity) < 1);
			if (dimmed.length) record.dimmed.push({ t: Math.round(performance.now()), nodes: dimmed.map((el) => el.tagName.toLowerCase() + (el.id ? `#${el.id}` : "")) });
			const marks = Object.fromEntries(Object.entries(landmarks).map(([key, selector]) => [key, rectOf(selector)]));
			record.frames.push({
				t: Math.round(performance.now()), theme: root.getAttribute("data-bs-theme"), sidebarSmall: root.classList.contains("ju-nav-sm"),
				ready: Object.values(marks).every(Boolean), marks,
			});
		}
		requestAnimationFrame(frame);
	};
	requestAnimationFrame(frame);
	new MutationObserver((mutations) => {
		for (const mutation of mutations) {
			if (mutation.type === "attributes" && mutation.target === document.documentElement && mutation.attributeName === "data-bs-theme") {
				record.themeWrites.push({ t: Math.round(performance.now()), from: mutation.oldValue, to: document.documentElement.getAttribute("data-bs-theme") });
			}
			if (mutation.type === "childList") {
				for (const node of mutation.removedNodes) {
					if (node.nodeType === 1 && node.matches?.(".target-detail-page, .target-detail-heading, #target-detail-tabs, .target-detail-actions")) {
						record.removed.push(node.className || node.id);
					}
				}
			}
		}
	}).observe(document, { attributes: true, attributeOldValue: true, attributeFilter: ["data-bs-theme"], childList: true, subtree: true });
	const note = (kind) => (event) => {
		if (event.target.nodeType === 1 && event.target.matches?.(SHELL_SELECTOR)) record.transitions.push(`${kind} ${event.target.className} ${event.propertyName || event.animationName}`);
	};
	document.addEventListener("transitionrun", note("transition"), true);
	document.addEventListener("animationstart", note("animation"), true);
}

async function measure(page, action) {
	await action();
	await page.waitForFunction(() => document.getElementById("target-config") && !document.querySelector('.target-detail-page [aria-busy="true"]'), null, { timeout: 30000 });
	await page.waitForTimeout(WINDOW_MS);
	return page.evaluate(() => window.__stability);
}

function assertStable(record, { theme, sidebarCollapsed, label }) {
	const ready = record.frames.filter((frame) => frame.ready);
	assert.ok(ready.length > 5, `${label}: page never rendered all landmarks`);
	const first = ready[0];
	// Theme: right in the first frame, never rewritten afterwards.
	assert.ok(record.frames.every((frame) => frame.theme === theme), `${label}: theme attribute left ${theme}: ${JSON.stringify([...new Set(record.frames.map((frame) => frame.theme))])}`);
	assert.deepEqual(record.themeWrites.filter((write) => write.t > first.t), [], `${label}: data-bs-theme written after first paint`);
	assert.ok(ready.every((frame) => frame.sidebarSmall === sidebarCollapsed), `${label}: sidebar state differs from the stored one`);
	assert.deepEqual(record.dimmed, [], `${label}: shell opacity dropped below 1 (whole-page flicker)`);
	assert.deepEqual(record.transitions, [], `${label}: shell elements transition or animate while loading`);
	assert.deepEqual(record.removed, [], `${label}: page structure was replaced after render`);
	// Landmarks visible in the first complete frame keep their position.
	const moved = [];
	for (const frame of ready) {
		for (const [key, rect] of Object.entries(frame.marks)) {
			const reference = first.marks[key];
			if (rect.some((value, index) => Math.abs(value - reference[index]) > LANDMARK_TOLERANCE_PX)) moved.push(`${frame.t}ms ${key} ${reference} -> ${rect}`);
		}
	}
	assert.deepEqual(moved.slice(0, 6), [], `${label}: landmarks moved after first paint (${moved.length} frames)`);
}

for (const viewport of VIEWPORTS) {
	for (const theme of THEMES) {
		for (const sidebarCollapsed of viewport.w >= 992 ? [false, true] : [false]) {
			const name = `target detail load stability ${theme} ${viewport.w}x${viewport.h} ${sidebarCollapsed ? "rail" : "expanded"} @load-stability`;
			test(name, async ({ browser, browserName, app, adminState }) => {
				const context = await browser.newContext({ viewport: { width: viewport.w, height: viewport.h }, storageState: adminState, colorScheme: theme });
				await context.addInitScript(recorder, sidebarCollapsed);
				const page = await context.newPage();
				try {
					const url = app.base + TARGET_PATH;
					const cold = await measure(page, () => page.goto(url, { waitUntil: "load" }));
					const options = { theme, sidebarCollapsed };
					assertStable(cold, { ...options, label: `${browserName} cold` });
					if (browserName === "chromium") assert.ok(cold.shifts <= CLS_BUDGET, `cold CLS ${cold.shifts.toFixed(4)} exceeds ${CLS_BUDGET}`);
					const warm = await measure(page, () => page.reload({ waitUntil: "load" }));
					assertStable(warm, { ...options, label: `${browserName} refresh` });
					if (browserName === "chromium") assert.ok(warm.shifts <= CLS_BUDGET, `refresh CLS ${warm.shifts.toFixed(4)} exceeds ${CLS_BUDGET}`);
				} finally {
					await context.close();
				}
			});
		}
	}
}
