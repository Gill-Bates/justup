//
// tests/layout/focus-style.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// DESIGN.md Focus Policy DC-FOCUS-1..8 (variants V1-V4) and the control rules DC-INT-*: a quiet but always visible focus
// indicator (no blue glow ring), switches with equal geometry in both states and a visible off state,
// and one cursor convention for every operable control. Measured with getComputedStyle, not by eye.
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { test as base } from "@playwright/test";
import { contrastRatio as contrast, test as appTest } from "./fixtures.mjs";
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
<div class="p-2" id="w-select-dis"><select id="select-dis" class="form-select" aria-label="Disabled select" disabled><option>A</option></select></div>
<div class="p-2" id="w-textarea"><textarea id="textarea" class="form-control" aria-label="Textarea"></textarea></div>
<div class="p-2" id="w-checkbox"><div class="form-check"><input id="checkbox" class="form-check-input" type="checkbox"><label class="form-check-label" for="checkbox">Check</label></div></div>
<div class="p-2" id="w-radio"><div class="form-check"><input id="radio" class="form-check-input" type="radio" name="r"><label class="form-check-label" for="radio">Radio</label></div></div>
<div class="p-2" id="w-switch"><div class="form-check form-switch ju-switch"><input id="switch" class="form-check-input" type="checkbox" role="switch"><label class="form-check-label" for="switch">Switch</label></div></div>
<div class="p-2" id="w-button"><button id="button" type="button" class="btn btn-primary">Button</button> <button id="button-outline" type="button" class="btn btn-outline-secondary">Outline</button></div>
<div class="p-2" id="w-link"><a id="link" href="#link">Link</a></div>
<div class="p-2" id="w-range"><input id="range" class="form-range" type="range" aria-label="Range"></div>
<div class="p-2" id="w-viewport"><div id="viewport" class="ju-table-viewport" tabindex="0" aria-label="Scroll region">Scroll region</div></div>
<div class="p-2" id="sw">
<div class="form-check form-switch"><input id="sw-off" class="form-check-input" type="checkbox" role="switch"><label class="form-check-label" for="sw-off">Off</label></div>
<div class="form-check form-switch"><input id="sw-on" class="form-check-input" type="checkbox" role="switch" checked><label class="form-check-label" for="sw-on">On</label></div>
<div class="form-check form-switch"><input id="sw-off-dis" class="form-check-input" type="checkbox" role="switch" disabled><label class="form-check-label" for="sw-off-dis">Off disabled</label></div>
<div class="form-check form-switch"><input id="sw-on-dis" class="form-check-input" type="checkbox" role="switch" checked disabled><label class="form-check-label" for="sw-on-dis">On disabled</label></div>
</div></div></main></body></html>`;
const FIELDS = ["text", "search", "password", "select", "textarea"];
const CONTROLS = ["checkbox", "radio", "switch", "button", "button-outline", "link", "range", "viewport"];
// DC-FOCUS-6: V2 draws the outline outside the control, V3 (inset) inside it; colour and width are shared.
const INSET = new Set(["viewport"]);
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
		if (await page.evaluate((x) => document.activeElement?.id === x, id)) {
			// Reduced motion leaves a 0.01 ms transition: measure the settled state, not its first frame.
			await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
			return;
		}
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
			assert.equal(after.outlineStyle, "none", `${id}: V1 text fields show focus by the border alone (DC-FOCUS-1), outline ${after.outlineStyle}`);
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
			const ringColour = await page.evaluate(() => { const probe = document.createElement("i"); probe.style.color = "var(--ju-focus-ring)"; document.body.append(probe); const colour = getComputedStyle(probe).color; probe.remove(); return colour; });
			assert.equal(after.outlineColor, ringColour, `${id}: outline colour is not the shared --ju-focus-ring`);
			if (INSET.has(id)) assert.ok(after.outlineOffset < 0, `${id}: inset variant needs a negative offset (${after.outlineOffset})`);
			else assert.equal(after.outlineOffset, 2, `${id}: outline offset`);
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
		assert.equal(await cursor("#select"), "pointer", "an operable select shows a hand");
		for (const sel of ["#select-dis", "#sw-off-dis", "#sw-on-dis", "label[for=sw-off-dis]", "label[for=sw-on-dis]"]) {
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
					const label = input.labels?.[0];
					const min = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--ui-target-min")) * parseFloat(getComputedStyle(document.documentElement).fontSize);
					out.switches.push({ id: input.id || input.name, role: input.getAttribute("role"), inJuSwitch: Boolean(input.closest(".ju-switch")), dw: Math.abs(before.width - after.width), dh: Math.abs(before.height - after.height), labelHeight: label ? label.getBoundingClientRect().height : null, min });
				}
				const operable = "button:not(:disabled), select:not(:disabled), a[href], summary, .form-check-input:not(:disabled), [role=tab], [role=button]:not([aria-disabled=true]), .form-check-input:not(:disabled) ~ .form-check-label, label.form-check-label[for]";
				for (const el of document.querySelectorAll(operable)) {
					if (el.matches(SPECIAL) || el.closest(SPECIAL)) continue;
					if (el.matches(".form-check-label") && el.control?.disabled) continue;
					out.cursors.push({ node: `${el.tagName.toLowerCase()}#${el.id || ""}.${String(el.className).split(" ")[0]}`, cursor: getComputedStyle(el).cursor });
				}
				return out;
			});
			for (const item of result.switches) assert.ok(item.dw <= 1 && item.dh <= 1, `${entry.id}: switch ${item.id} changes size when toggled (${item.dw}x${item.dh})`);
			// DC-FORM-5 (N21): a switch is a role=switch checkbox inside .ju-switch.
			const plain = result.switches.filter((item) => item.role !== "switch" || !item.inJuSwitch).map((item) => item.id);
			assert.deepEqual(plain, [], `${entry.id}: switches without role=switch or outside .ju-switch`);
			// DC-FORM-5: the label of a switch is a pointer target of at least --ui-target-min.
			for (const item of result.switches) if (item.labelHeight !== null) assert.ok(item.labelHeight >= item.min - 0.5, `${entry.id}: label of switch ${item.id} is ${item.labelHeight}px high, below ${item.min}px`);
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
// DC-EX-FOCUS-1 / DC-FOCUS-7: `main` is a programmatic focus target (skip link) and shows no ring.
for (const theme of THEMES) {
	appTest(`skip link moves focus to main without a ring (${theme}) @focus`, async ({ browser, app, adminState }) => {
		const context = await browser.newContext({ viewport: { width: 1280, height: 720 }, storageState: adminState });
		try {
			const page = await context.newPage();
			await page.goto(app.base + "/ui/targets", { waitUntil: "load" });
			await setTheme(page, theme);
			await page.keyboard.press("Tab");
			await page.keyboard.press("Enter");
			await page.waitForFunction(() => document.activeElement === document.querySelector("main"));
			const outline = await page.evaluate(() => { const cs = getComputedStyle(document.querySelector("main")); return { style: cs.outlineStyle, width: cs.outlineWidth }; });
			assert.ok(outline.style === "none" || outline.width === "0px", `main shows an outline after the skip link: ${JSON.stringify(outline)}`);
		} finally {
			await context.close();
		}
	});
}

// DC-SCROLL-5 (N53): the scrolling dependency list is a labelled, focusable region.
for (const theme of THEMES) {
	appTest(`about: the dependencies scroll region is a labelled focusable region (${theme}) @focus`, async ({ browser, app, adminState }) => {
		const context = await browser.newContext({ viewport: { width: 1280, height: 720 }, storageState: adminState });
		try {
			const page = await context.newPage();
			await page.goto(app.base + "/ui/about", { waitUntil: "load" });
			await setTheme(page, theme);
			const region = page.locator(".dependencies-scroll");
			assert.equal(await region.getAttribute("role"), "region");
			assert.ok((await region.getAttribute("aria-label"))?.trim(), "the region has an accessible name");
			assert.equal(await region.getAttribute("tabindex"), "0");
			await page.keyboard.press("Tab"); // reset keyboard modality so :focus-visible applies to the focus() call
			await region.focus();
			assert.equal(await region.evaluate((el) => el.matches(":focus-visible")), true);
		} finally {
			await context.close();
		}
	});
}

// N16 / DC-FOCUS-3: forced colors drop box-shadow and author colours, so every control a keyboard user reaches on real pages
// must keep an outline of its own, of an enclosing element that holds the focus (tag-input field, type card) or of its
// stretched-link ::after.
const FORCED_FOCUS_PAGES = [{ id: "target-edit", path: "/ui/targets/1/edit", min: 20 }, { id: "wizard", path: "/ui/targets/new", min: 3 }, { id: "notifications", path: "/ui/notifications", min: 20 }];
for (const entry of FORCED_FOCUS_PAGES) {
	appTest(`${entry.id}: forced colors keep a focus outline on every tabbable control @focus`, async ({ browser, app, adminState }) => {
		const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, storageState: adminState, forcedColors: "active" });
		try {
			const page = await context.newPage();
			await page.goto(app.base + entry.path, { waitUntil: "load" });
			assert.equal(await page.evaluate(() => window.matchMedia("(forced-colors: active)").matches), true, "forced colors emulation is active");
			const stops = [];
			for (let press = 0; press < 160; press += 1) {
				await page.keyboard.press("Tab");
				const stop = await page.evaluate(() => {
					const el = document.activeElement;
					if (!el || el === document.body) return null;
					const drawn = (node, pseudo) => { const cs = getComputedStyle(node, pseudo); return cs.outlineStyle !== "none" && parseFloat(cs.outlineWidth) >= 2; };
					let ok = drawn(el, "::after");
					for (let node = el, depth = 0; !ok && node && depth < 4 && !node.matches("main, body, html"); node = node.parentElement, depth += 1) ok = drawn(node);
					return { node: `${el.tagName.toLowerCase()}${el.id ? `#${el.id}` : ""}.${String(el.getAttribute("class") ?? "").split(" ")[0]}`, ok };
				});
				if (!stop) break;
				stops.push(stop);
			}
			assert.ok(stops.length >= entry.min, `${entry.id}: the Tab sweep reached ${stops.length} controls`);
			assert.deepEqual([...new Set(stops.filter((stop) => !stop.ok).map((stop) => stop.node))], [], `${entry.id}: controls without a focus outline in forced colors`);
			console.log(`[forced-colors] ${entry.id}: ${stops.map((stop) => stop.node).join(" ")}`);
		} finally {
			await context.close();
		}
	});
}
// ---- OR-1 / DC-FOCUS-6: the focus variants on the real pages ----
// The ring of a rule group is compared with the tokens as the browser resolves them, not with literals.
const resolveTokens = (page) => page.evaluate(() => {
	const probe = document.createElement("span");
	probe.style.cssText = "outline: var(--ju-focus-outline-width) solid var(--ju-focus-ring); outline-offset: var(--ju-focus-outline-offset); position: absolute; visibility: hidden;";
	document.body.append(probe);
	const cs = getComputedStyle(probe);
	const out = { width: parseFloat(cs.outlineWidth), colour: cs.outlineColor, offset: parseFloat(cs.outlineOffset) };
	probe.remove();
	return out;
});
const ringOf = (page, selector) => page.locator(selector).first().evaluate((el) => {
	const cs = getComputedStyle(el);
	return { style: cs.outlineStyle, width: parseFloat(cs.outlineWidth), colour: cs.outlineColor, offset: parseFloat(cs.outlineOffset), boxShadow: cs.boxShadow, opacity: cs.opacity, focusVisible: el.matches(":focus-visible") };
});
const assertRing = (ring, tokens, { inset, label, selectedFill = false }) => {
	assert.equal(ring.style, "solid", `${label}: outline style`);
	assert.equal(ring.width, tokens.width, `${label}: outline width reads --ju-focus-outline-width`);
	assert.equal(ring.colour, tokens.colour, `${label}: outline colour reads --ju-focus-ring`);
	assert.equal(ring.offset, inset ? -tokens.width : tokens.offset, `${label}: ${inset ? "V3 offset is the negative width" : "V2/V4 offset reads --ju-focus-outline-offset"}`);
	assert.ok(tokens.offset > 0, "the shared V2 offset is positive");
	// A selected control may carry its own inset fill cue (DC-FOCUS-5); an unselected one has no shadow at all.
	if (!selectedFill) assert.equal(ring.boxShadow, "none", `${label}: no box-shadow ring`);
};
for (const theme of THEMES) {
	// V4: the wrapper of a radio (type card) draws the ring through :focus-within; the inner input draws nothing.
	appTest(`wizard: the focused type card draws the V4 ring on the card, not on its radio (${theme}) @focus`, async ({ browser, app, adminState }) => {
		const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, storageState: adminState });
		try {
			const page = await context.newPage();
			await page.goto(`${app.base}/ui/targets/new`, { waitUntil: "load" });
			await setTheme(page, theme);
			const tokens = await resolveTokens(page);
			await page.keyboard.press("Tab"); // reset keyboard modality
			await page.locator(".target-type-radio:not(:checked)").first().focus();
			const card = await ringOf(page, ".target-type-card:focus-within");
			assertRing(card, tokens, { inset: false, label: "type card" });
			const radio = await ringOf(page, ".target-type-radio:focus");
			// The radio is a 1 px, fully transparent input: its (browser default) outline is not painted.
			assert.ok(radio.style === "none" || radio.width === 0 || radio.opacity === "0", `the radio inside the card paints its own outline: ${JSON.stringify(radio)}`);
			assert.equal(await page.locator(".target-type-card:focus-within").count(), 1, "exactly one card shows the ring");
		} finally {
			await context.close();
		}
	});
	// V4: the tag input field is the wrapper of the bare entry input.
	appTest(`settings: the focused tag input draws the V4 ring on its field, not on the entry (${theme}) @focus`, async ({ browser, app, adminState }) => {
		const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, storageState: adminState });
		try {
			const page = await context.newPage();
			await page.goto(`${app.base}/ui/settings`, { waitUntil: "load" });
			await setTheme(page, theme);
			await page.click("#network-tab");
			const entry = page.locator("#quality_targets_entry");
			await entry.waitFor({ state: "visible" });
			const tokens = await resolveTokens(page);
			await page.keyboard.press("Tab"); // reset keyboard modality
			await entry.focus();
			const field = await ringOf(page, ".tag-input__field:focus-within");
			assertRing(field, tokens, { inset: false, label: "tag input field" });
			const inner = await ringOf(page, "#quality_targets_entry");
			assert.ok(inner.style === "none" || inner.width === 0 || inner.opacity === "0", `the entry inside the field paints its own outline: ${JSON.stringify(inner)}`);
			assert.equal(inner.boxShadow, "none", "the entry draws no shadow ring");
		} finally {
			await context.close();
		}
	});
}
// ---- DC-FOCUS-5: a selected or active state has its own fill and is not a focus indicator; focused and selected show both ----
for (const theme of THEMES) {
	appTest(`wizard: a focused and selected type card shows the ring and the selected fill (${theme}) @focus`, async ({ browser, app, adminState }) => {
		const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, storageState: adminState });
		try {
			const page = await context.newPage();
			await page.goto(`${app.base}/ui/targets/new`, { waitUntil: "load" });
			await setTheme(page, theme);
			const tokens = await resolveTokens(page);
			const fill = (selector) => page.locator(selector).first().evaluate((el) => { const cs = getComputedStyle(el); return { background: cs.backgroundColor, border: cs.borderTopColor, shadow: cs.boxShadow }; });
			await page.keyboard.press("Tab");
			await page.evaluate(() => document.activeElement?.blur()); // the wizard focuses the selected card on load
			// Not focused: the selected card differs from an unselected one by its own fill, without any outline.
			const selectedAtRest = await fill(".target-type-card:has(.target-type-radio:checked)");
			const plain = await fill(".target-type-card:not(:has(.target-type-radio:checked))");
			assert.notEqual(selectedAtRest.background, plain.background, "the selected card has its own fill");
			assert.equal(await ringOf(page, ".target-type-card:has(.target-type-radio:checked)").then((ring) => ring.style), "none", "selection alone draws no outline");
			await page.locator(".target-type-radio:checked").first().focus();
			const ring = await ringOf(page, ".target-type-card:has(.target-type-radio:checked)");
			assertRing(ring, tokens, { inset: false, label: "focused and selected card", selectedFill: true });
			const focusedSelected = await fill(".target-type-card:has(.target-type-radio:checked)");
			assert.equal(focusedSelected.background, selectedAtRest.background, "focus keeps the selected fill");
			assert.notEqual(focusedSelected.shadow, "none", "focus keeps the selected cue");
		} finally {
			await context.close();
		}
	});
	appTest(`settings: the focused active tab keeps its fill and gets the ring (${theme}) @focus`, async ({ browser, app, adminState }) => {
		const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, storageState: adminState });
		try {
			const page = await context.newPage();
			await page.goto(`${app.base}/ui/settings`, { waitUntil: "load" });
			await setTheme(page, theme);
			const tokens = await resolveTokens(page);
			const paint = (selector) => page.locator(selector).first().evaluate((el) => getComputedStyle(el).backgroundColor);
			const activeFill = await paint("#settingsTabs .nav-link.active");
			const idleFill = await paint("#settingsTabs .nav-link:not(.active)");
			assert.notEqual(activeFill, idleFill, "the active tab has its own fill");
			assert.equal((await ringOf(page, "#settingsTabs .nav-link.active")).style, "none", "an unfocused active tab draws no outline");
			await page.keyboard.press("Tab");
			await page.locator("#settingsTabs .nav-link.active").focus();
			const ring = await ringOf(page, "#settingsTabs .nav-link.active");
			assert.equal(ring.focusVisible, true, "the active tab is focus-visible");
			assertRing(ring, tokens, { inset: false, label: "focused active tab" });
			assert.equal(await paint("#settingsTabs .nav-link.active"), activeFill, "focus keeps the active fill");
		} finally {
			await context.close();
		}
	});
}
