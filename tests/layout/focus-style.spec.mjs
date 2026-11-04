//
// tests/layout/focus-style.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// DESIGN.md "Focus indication" and "Controls: cursor and switch geometry": a quiet but always visible focus
// indicator (no blue glow ring), switches with equal geometry in both states and a visible off state,
// and one cursor convention for every operable control. Measured with getComputedStyle, not by eye.
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { test as base } from "@playwright/test";
import { test as appTest } from "./fixtures.mjs";
import { startStaticAppServer } from "./test-support.mjs";
const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), "..", "..");
const THEMES = ["light", "dark"];
const SHOTS = process.env.JUSTUP_FOCUS_SHOTS;
const PAGE = `<!doctype html><html lang="en" data-bs-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/static/css/bootstrap.min.css"><link rel="stylesheet" href="/static/css/gentelella-layout.css"><link rel="stylesheet" href="/static/css/ui.css">
</head><body><main class="p-3"><div id="surface" class="p-3" style="background:var(--ju-panel-bg)">
<a id="start" href="#start">start</a>
<div class="p-2" id="w-text"><input id="text" class="form-control" type="text" aria-label="Text"></div>
<div class="p-2" id="w-search"><input id="search" class="form-control" type="search" aria-label="Search"></div>
<div class="p-2" id="w-password"><input id="password" class="form-control" type="password" aria-label="Password"></div>
<div class="p-2" id="w-select"><select id="select" class="form-select" aria-label="Select"><option>A</option><option>B</option></select></div>
<div class="p-2" id="w-textarea"><textarea id="textarea" class="form-control" aria-label="Textarea"></textarea></div>
<div class="p-2" id="w-checkbox"><div class="form-check"><input id="checkbox" class="form-check-input" type="checkbox"><label class="form-check-label" for="checkbox">Check</label></div></div>
<div class="p-2" id="w-radio"><div class="form-check"><input id="radio" class="form-check-input" type="radio" name="r"><label class="form-check-label" for="radio">Radio</label></div></div>
<div class="p-2" id="w-switch"><div class="form-check form-switch ju-switch"><input id="switch" class="form-check-input" type="checkbox" role="switch"><label class="form-check-label" for="switch">Switch</label></div></div>
<div class="p-2" id="w-button"><button id="button" type="button" class="btn btn-primary">Button</button> <button id="button-outline" type="button" class="btn btn-outline-secondary">Outline</button></div>
<div class="p-2" id="w-link"><a id="link" href="#link">Link</a></div>
<div class="p-2" id="sw">
<div class="form-check form-switch"><input id="sw-off" class="form-check-input" type="checkbox" role="switch"><label class="form-check-label" for="sw-off">Off</label></div>
<div class="form-check form-switch"><input id="sw-on" class="form-check-input" type="checkbox" role="switch" checked><label class="form-check-label" for="sw-on">On</label></div>
<div class="form-check form-switch"><input id="sw-off-dis" class="form-check-input" type="checkbox" role="switch" disabled><label class="form-check-label" for="sw-off-dis">Off disabled</label></div>
<div class="form-check form-switch"><input id="sw-on-dis" class="form-check-input" type="checkbox" role="switch" checked disabled><label class="form-check-label" for="sw-on-dis">On disabled</label></div>
</div></div></main></body></html>`;
const FIELDS = ["text", "search", "password", "select", "textarea"];
const CONTROLS = ["checkbox", "radio", "switch", "button", "button-outline", "link"];
const SWITCHES = ["sw-off", "sw-on", "sw-off-dis", "sw-on-dis"];
const test = base.extend({
	base: [async ({}, use) => {
		const server = await startStaticAppServer({ root: ROOT, renderPage: () => PAGE });
		try { await use(server.baseUrl); } finally { await server.close(); }
	}, { scope: "worker" }],
});
// ---- colour helpers (computed colours arrive as rgb()/rgba()) ----
const parse = (value) => {
	const m = /rgba?\(([^)]+)\)/.exec(value);
	assert.ok(m, `unparsable colour: ${value}`);
	const [r, g, b, a = 1] = m[1].split(/[ ,/]+/).filter(Boolean).map(Number);
	return { r, g, b, a };
};
const lum = ({ r, g, b }) => {
	const f = (c) => { const s = c / 255; return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4; };
	return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
};
const contrast = (a, b) => { const [x, y] = [lum(parse(a)), lum(parse(b))].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05); };
const isBlue = (value) => { const c = parse(value); return c.a > 0 && c.b > c.g + 40 && c.b > c.r + 80; };
const setTheme = async (page, theme) => {
	await page.emulateMedia({ reducedMotion: "reduce" }); // ends colour transitions at once so measurements are stable
	await page.evaluate((t) => document.documentElement.setAttribute("data-bs-theme", t), theme);
	await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
};
// Computed style of one element plus the first opaque background behind it.
const measure = (page, selector) => page.evaluate((sel) => {
	const el = document.querySelector(sel);
	const cs = getComputedStyle(el);
	let surface = el.parentElement;
	while (surface && Number(/rgba?\([^)]*?,?\s*([\d.]+)\)$/.exec(getComputedStyle(surface).backgroundColor)?.[1] ?? 1) === 0) surface = surface.parentElement;
	const rect = el.getBoundingClientRect();
	return {
		boxShadow: cs.boxShadow, outlineStyle: cs.outlineStyle, outlineWidth: parseFloat(cs.outlineWidth), outlineColor: cs.outlineColor,
		outlineOffset: parseFloat(cs.outlineOffset), borderColor: cs.borderTopColor, borderWidth: parseFloat(cs.borderTopWidth),
		background: cs.backgroundColor, backgroundImage: cs.backgroundImage, backgroundSize: cs.backgroundSize,
		surface: getComputedStyle(surface || document.documentElement).backgroundColor, width: rect.width, height: rect.height,
		transition: cs.transitionDuration, cursor: cs.cursor, focusVisible: el.matches(":focus-visible"),
	};
}, selector);
const tabTo = async (page, id) => {
	await page.focus("#start");
	for (let n = 0; n < 40; n += 1) {
		await page.keyboard.press("Tab");
		if (await page.evaluate((x) => document.activeElement?.id === x, id)) return;
	}
	throw new Error(`Tab never reached #${id}`);
};
const shot = async (page, id, theme, state, browserName) => {
	if (!SHOTS) return;
	fs.mkdirSync(SHOTS, { recursive: true });
	await page.locator(`#w-${id.split("-")[0]}`).screenshot({ path: path.join(SHOTS, `${browserName}-${theme}-${id}-${state}.png`) });
};
const noGlow = (after, label) => {
	assert.equal(after.boxShadow, "none", `${label}: box-shadow ring ${after.boxShadow}`);
	assert.ok(after.outlineWidth <= 2, `${label}: outline wider than 2px (${after.outlineWidth})`);
	for (const value of [after.outlineColor, after.borderColor]) assert.ok(!isBlue(value), `${label}: blue focus colour ${value}`);
};
for (const theme of THEMES) {
	test(`text-like fields use a quiet border change, no glow (${theme}) @focus`, async ({ page, base, browserName }) => {
		await page.goto(base);
		await setTheme(page, theme);
		for (const id of FIELDS) {
			const before = await measure(page, `#${id}`);
			await tabTo(page, id);
			const after = await measure(page, `#${id}`);
			await shot(page, id, theme, "focus", browserName);
			noGlow(after, id);
			assert.ok(after.focusVisible, `${id}: expected :focus-visible after Tab`);
			assert.notEqual(after.borderColor, before.borderColor, `${id}: focus must change the border`);
			const edge = Math.min(contrast(after.borderColor, after.background), contrast(after.borderColor, after.surface));
			console.log(`[contrast] ${browserName} ${theme} ${id} focus edge ${edge.toFixed(2)}:1 (${after.borderColor})`);
			assert.ok(edge >= 3, `${id}: focus edge contrast ${edge.toFixed(2)} < 3`);
			assert.ok(after.borderWidth <= 1.01, `${id}: border grew to ${after.borderWidth}px`);
			// A pointer click gives the same quiet indicator.
			await page.click(`#${id}`);
			const clicked = await measure(page, `#${id}`);
			noGlow(clicked, `${id} (click)`);
			assert.equal(clicked.borderColor, after.borderColor, `${id}: click and Tab differ`);
		}
	});
	test(`buttons, switches, checkboxes and links keep a narrow neutral outline on keyboard focus (${theme}) @focus`, async ({ page, base, browserName }) => {
		await page.goto(base);
		await setTheme(page, theme);
		for (const id of CONTROLS) {
			await tabTo(page, id);
			const after = await measure(page, `#${id}`);
			await shot(page, id, theme, "focus-visible", browserName);
			noGlow(after, id);
			assert.ok(after.focusVisible, `${id}: expected :focus-visible after Tab`);
			assert.notEqual(after.outlineStyle, "none", `${id}: no outline`);
			assert.equal(after.outlineWidth, 2, `${id}: outline width`);
			const ring = contrast(after.outlineColor, after.surface);
			console.log(`[contrast] ${browserName} ${theme} ${id} outline ${ring.toFixed(2)}:1 (${after.outlineColor})`);
			assert.ok(ring >= 3, `${id}: outline contrast ${ring.toFixed(2)} < 3`);
		}
		// Pointer focus on a checkbox or switch draws neither ring nor blue border.
		for (const id of ["checkbox", "switch", "radio"]) {
			await page.click(`#${id}`);
			const clicked = await measure(page, `#${id}`);
			assert.equal(clicked.boxShadow, "none", `${id} (click): box-shadow`);
			assert.ok(!isBlue(clicked.borderColor), `${id} (click): blue border ${clicked.borderColor}`);
			await page.click(`#${id}`);
		}
	});
	test(`forced colors keep the outline (${theme}) @focus`, async ({ page, base, browserName }) => {
		await page.goto(base);
		await setTheme(page, theme);
		try { await page.emulateMedia({ forcedColors: "active" }); } catch (error) { test.skip(true, `forced colors emulation unavailable in ${browserName}: ${error.message}`); }
		for (const id of [...FIELDS, ...CONTROLS]) {
			await tabTo(page, id);
			const after = await measure(page, `#${id}`);
			assert.notEqual(after.outlineStyle, "none", `${id}: outline vanished in forced colors`);
			assert.ok(after.outlineWidth >= 2, `${id}: outline too thin in forced colors`);
		}
	});
	test(`switch geometry is equal in both states and the off state is visible (${theme}) @focus`, async ({ page, base, browserName }) => {
		await page.goto(base);
		await setTheme(page, theme);
		const off = await measure(page, "#sw-off");
		const on = await measure(page, "#sw-on");
		await shot(page, "switch", theme, "state", browserName);
		for (const key of ["width", "height", "borderWidth"]) assert.ok(Math.abs(off[key] - on[key]) <= 1, `${key}: off ${off[key]} vs on ${on[key]}`);
		const thumb = (m) => decodeURIComponent(m.backgroundImage).match(/<circle[^>]*>/)?.[0] ?? "";
		const radius = (m) => /r=['"]?([\d.]+)/.exec(thumb(m))?.[1];
		assert.ok(radius(off) && radius(off) === radius(on), `thumb diameter: off ${thumb(off)} vs on ${thumb(on)}`);
		assert.equal(off.backgroundSize, on.backgroundSize, "thumb box size differs");
		// The off track edge and thumb must be visible against the surface (WCAG 1.4.11).
		const edge = contrast(off.borderColor, off.surface);
		const fill = /fill=['"]([^'"]+)['"]/.exec(thumb(off))?.[1] ?? "";
		const fillColour = /^#[0-9a-f]{6}$/i.test(fill) ? `rgb(${[1, 3, 5].map((i) => parseInt(fill.slice(i, i + 2), 16)).join(",")})` : "";
		const thumbRatio = fillColour ? contrast(fillColour, off.background) : 0;
		console.log(`[contrast] ${browserName} ${theme} switch off edge ${edge.toFixed(2)}:1 thumb ${thumbRatio.toFixed(2)}:1 size ${off.width}x${off.height} on ${on.width}x${on.height}`);
		assert.ok(edge >= 3, `off switch edge contrast ${edge.toFixed(2)} < 3`);
		assert.ok(thumbRatio >= 3, `off switch thumb contrast ${thumbRatio.toFixed(2)} < 3 (${fill})`);
		assert.ok(contrast(on.background, on.surface) >= 3, "on switch track contrast < 3");
	});
	test(`cursor convention on a control gallery (${theme}) @focus`, async ({ page, base }) => {
		await page.goto(base);
		await setTheme(page, theme);
		const cursor = async (selector) => (await measure(page, selector)).cursor;
		for (const sel of ["#button", "#link", "#checkbox", "#switch", "#radio", "#sw-on", "label[for=switch]", "label[for=checkbox]", "label[for=radio]"]) {
			assert.equal(await cursor(sel), "pointer", `${sel} should show a hand`);
		}
		for (const sel of ["#sw-off-dis", "#sw-on-dis", "label[for=sw-off-dis]", "label[for=sw-on-dis]"]) {
			assert.equal(await cursor(sel), "not-allowed", `${sel} (disabled) should show not-allowed`);
		}
		for (const sel of ["#text", "#search", "#password", "#textarea"]) assert.ok(["text", "auto"].includes(await cursor(sel)), `${sel} keeps the native text cursor`);
	});
}
// ---- real pages: every switch keeps its box when toggled; cursors of all operable controls ----
const PAGES = [{ id: "wizard", path: "/ui/targets/new" }, { id: "settings", path: "/ui/settings" }, { id: "target-edit", path: "/ui/targets/1/edit" }, { id: "target-detail", path: "/ui/targets/1" }];
for (const theme of THEMES) {
	for (const entry of PAGES) {
		appTest(`${entry.id}: switches keep their box and operable controls show a hand (${theme}) @focus`, async ({ browser, app, adminState, browserName }) => {
			const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, storageState: adminState });
			const page = await context.newPage();
			await page.goto(app.base + entry.path, { waitUntil: "load" });
			await setTheme(page, theme);
			const result = await page.evaluate(() => {
				const out = { switches: [], cursors: [] };
				const SPECIAL = ".dashboard-widget-handle, .ui-resizable-handle, [class*='resizable'], .leaflet-container *, .tag-input__entry, [draggable='true']";
				for (const input of document.querySelectorAll(".form-switch .form-check-input")) {
					const before = input.getBoundingClientRect();
					if (!before.width) continue; // hidden group or closed dialog
					const checked = input.checked;
					input.checked = !checked;
					const after = input.getBoundingClientRect();
					input.checked = checked;
					out.switches.push({ id: input.id || input.name, dw: Math.abs(before.width - after.width), dh: Math.abs(before.height - after.height) });
				}
				const operable = "button:not(:disabled), a[href], summary, .form-check-input:not(:disabled), [role=tab], [role=button]:not([aria-disabled=true]), .form-check-input:not(:disabled) ~ .form-check-label, label.form-check-label[for]";
				for (const el of document.querySelectorAll(operable)) {
					if (el.matches(SPECIAL) || el.closest(SPECIAL)) continue;
					if (el.matches(".form-check-label") && el.control?.disabled) continue;
					out.cursors.push({ node: `${el.tagName.toLowerCase()}#${el.id || ""}.${String(el.className).split(" ")[0]}`, cursor: getComputedStyle(el).cursor });
				}
				return out;
			});
			for (const item of result.switches) assert.ok(item.dw <= 1 && item.dh <= 1, `${entry.id}: switch ${item.id} changes size when toggled (${item.dw}x${item.dh})`);
			const wrong = result.cursors.filter((item) => item.cursor !== "pointer");
			assert.deepEqual(wrong, [], `${entry.id}: operable controls without a hand cursor`);
			console.log(`[cursor] ${browserName} ${theme} ${entry.id}: ${result.switches.length} switches, ${result.cursors.length} operable controls`);
			await context.close();
		});
	}
	appTest(`targets search field uses the quiet focus (${theme}) @focus`, async ({ browser, app, adminState, browserName }) => {
		const context = await browser.newContext({ viewport: { width: 1280, height: 720 }, storageState: adminState });
		const page = await context.newPage();
		await page.goto(app.base + "/ui/targets", { waitUntil: "load" });
		await setTheme(page, theme);
		const field = page.locator("input[type=search]").first();
		await field.focus();
		const after = await field.evaluate((el) => { const cs = getComputedStyle(el); return { boxShadow: cs.boxShadow, borderColor: cs.borderTopColor, outlineStyle: cs.outlineStyle, outlineColor: cs.outlineColor }; });
		if (SHOTS) { fs.mkdirSync(SHOTS, { recursive: true }); await field.locator("xpath=ancestor::*[1]").screenshot({ path: path.join(SHOTS, `${browserName}-${theme}-targets-search-focus.png`) }); }
		assert.equal(after.boxShadow, "none", "search field shows a ring");
		assert.ok(!isBlue(after.borderColor) && !isBlue(after.outlineColor), "search field uses a blue focus colour");
		await context.close();
	});
}
