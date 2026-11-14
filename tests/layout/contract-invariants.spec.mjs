//
// tests/layout/contract-invariants.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Contract invariants that no other spec proves on its own (DESIGN.md 10.1, DC-GATE-2):
//   DC-FORM-6..9  the field-error pattern of JuFieldErrors (aria-invalid, aria-describedby, no live region, focus, clearing)
//   DC-THEME-1    a theme switch through the real toggle updates every visible component without a reload or a layout shift
//   DC-THEME-2    forced colors: dots, state badges and the active tab use system colours
//   DC-TOK-6      reduced motion leaves no running non-essential animation (DC-THEME-3)
import assert from "node:assert/strict";
import path from "node:path";
import { test as base } from "@playwright/test";
import { test as appTest } from "./fixtures.mjs";
import { CFG } from "./harness.mjs";
import { startStaticAppServer } from "./test-support.mjs";
const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), "..", "..");
const FORM = `<!doctype html><html lang="en" data-bs-theme="light"><head><meta charset="utf-8">
<link rel="stylesheet" href="/static/css/bootstrap.min.css"><link rel="stylesheet" href="/static/css/ui.css">
</head><body><main class="p-3"><form id="scope" novalidate>
<div class="mb-3"><label for="name">Name</label><input id="name" class="form-control" type="text" aria-describedby="name-help"><div id="name-help" class="form-text">Help</div></div>
<div class="mb-3"><div class="input-group"><input id="host" class="form-control" type="text" aria-label="Host"><span class="input-group-text">:443</span></div></div>
<div class="form-check form-switch ju-switch"><input id="flag" class="form-check-input" type="checkbox" role="switch"><label class="form-check-label" for="flag">Flag</label></div>
</form></main><script src="/static/js/utils/field-errors.js"></script></body></html>`;
const formTest = base.extend({
	base: [async ({}, use) => {
		const server = await startStaticAppServer({ root: ROOT, renderPage: () => FORM });
		try { await use(server.baseUrl); } finally { await server.close(); }
	}, { scope: "worker" }],
});
const settled = (page) => page.waitForFunction(() => document.getAnimations({ subtree: true }).every((a) => a.effect?.getComputedTiming().iterations === Infinity || a.playState !== "running"), null, { timeout: 10000 });
const tokens = (page, id) => page.evaluate((x) => (document.getElementById(x).getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean), id);

formTest("field errors follow one pattern for text, input-group and switch controls @contract-invariants", async ({ page, base }) => {
	await page.goto(base);
	await page.waitForFunction(() => Boolean(window.JuFieldErrors));
	const errors = [{ id: "name", message: "Enter a name" }, { id: "host", message: "Enter a host" }, { id: "flag", message: "Confirm the flag" }];
	await page.evaluate((list) => window.JuFieldErrors.show(document.getElementById("scope"), list), errors);
	// DC-FORM-8: focus lands on the first invalid field.
	assert.equal(await page.evaluate(() => document.activeElement.id), "name");
	for (const { id, message } of errors) {
		const state = await page.evaluate((x) => {
			const input = document.getElementById(x);
			const el = document.getElementById(`${x}-field-error`);
			const anchor = input.closest(".ju-switch") || (input.parentElement.classList.contains("input-group") ? input.parentElement : input);
			return {
				invalid: input.getAttribute("aria-invalid"), marked: input.classList.contains("is-invalid"), exists: Boolean(el),
				text: el?.textContent ?? "", follows: anchor.nextElementSibling === el, role: el?.getAttribute("role"), live: el?.getAttribute("aria-live"),
				count: document.querySelectorAll(`#${x}-field-error`).length,
			};
		}, id);
		assert.equal(state.invalid, "true", `${id}: aria-invalid`);
		assert.ok(state.marked, `${id}: .is-invalid`);
		assert.ok(state.exists && state.text.includes(message), `${id}: error text`);
		assert.ok(state.follows, `${id}: the error sits directly after the control (DC-FORM-7)`);
		assert.equal(state.role, null, `${id}: a field error has no role`);
		assert.equal(state.live, null, `${id}: a field error has no aria-live`);
		assert.equal(state.count, 1, `${id}: exactly one error element`);
		assert.ok((await tokens(page, id)).includes(`${id}-field-error`), `${id}: aria-describedby names the error`);
	}
	// The help id stays next to the error id (DC-FORM-7).
	assert.deepEqual(await tokens(page, "name"), ["name-field-error", "name-help"]);
	// A second show() replaces the errors instead of stacking them.
	await page.evaluate(() => window.JuFieldErrors.show(document.getElementById("scope"), [{ id: "name", message: "Again" }]));
	assert.equal(await page.locator(".ju-field-error").count(), 1);
	assert.equal(await page.locator("#host").getAttribute("aria-invalid"), null, "show() clears errors of other fields");
	// DC-FORM-9: editing clears the error and restores the help-only description.
	await page.fill("#name", "x");
	await page.waitForFunction(() => !document.getElementById("name-field-error"));
	assert.equal(await page.locator("#name").getAttribute("aria-invalid"), null);
	assert.deepEqual(await tokens(page, "name"), ["name-help"]);
	// A switch clears on change; a control without help loses aria-describedby entirely.
	await page.evaluate(() => window.JuFieldErrors.show(document.getElementById("scope"), [{ id: "flag", message: "Confirm" }, { id: "host", message: "Host" }]));
	await page.locator("#flag").check();
	await page.waitForFunction(() => !document.getElementById("flag-field-error"));
	assert.equal(await page.locator("#flag").getAttribute("aria-describedby"), null);
	// set() marks without moving focus (DC-FORM-8: only a commit action moves focus).
	await page.evaluate(() => { document.getElementById("name").focus(); window.JuFieldErrors.set(document.getElementById("flag"), "Late"); });
	assert.equal(await page.evaluate(() => document.activeElement.id), "name");
});

const THEME_COMPONENTS = ".ju-topbar, .ju-sidebar, main .card, main .btn, main .nav-link, main .ju-status-dot, main .form-control";
const snapshot = (page) => page.evaluate((selector) => {
	const root = document.documentElement;
	const rects = [...document.querySelectorAll(selector)].filter((el) => el.getClientRects().length).slice(0, 60).map((el) => {
		const r = el.getBoundingClientRect();
		return [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)];
	});
	const card = document.querySelector("main .card");
	return {
		theme: root.getAttribute("data-bs-theme"), user: root.getAttribute("data-user-theme"), rects,
		bodyBg: getComputedStyle(document.body).backgroundColor, cardBg: card ? getComputedStyle(card).backgroundColor : null,
		wordmarkInverted: getComputedStyle(document.querySelector(".ju-topbar-brand .ju-logo")).filter.includes("invert(1)"),
		marker: window.__contractNoReload === true,
	};
}, THEME_COMPONENTS);

for (const width of [1280, 390]) {
	appTest(`theme switch updates visible components without reload or layout shift at ${width}px @contract-invariants`, async ({ browser, app, adminState }) => {
		const context = await browser.newContext({ viewport: { width, height: 844 }, storageState: adminState, colorScheme: "light" });
		try {
			const page = await context.newPage();
			// The toggle persists the choice; keep the shared admin account unchanged.
			await page.route("**/me/theme", (route) => route.fulfill({ status: 200, contentType: "application/json", body: "{}" }));
			await page.goto(`${app.base}/ui/targets`, { waitUntil: "load" });
			await page.waitForSelector("main .card, main .ju-status-dot");
			// Groups restore their stored collapsed state once the table is initialised (app.js initAccordionState),
			// so the number of visible components depends on earlier specs. Wait for that step, then open every
			// group, so both snapshots see the same populated page.
			await page.waitForFunction(() => {
				const groups = document.querySelectorAll("#targets-accordion .accordion-collapse");
				return groups.length > 0 && [...groups].every((el) => el.hasAttribute("data-state-init"));
			});
			await page.evaluate(() => {
				document.querySelectorAll("#targets-accordion .accordion-collapse").forEach((collapse) => {
					const button = document.querySelector(`[data-bs-target="#${collapse.id}"]`);
					collapse.classList.add("show");
					button?.setAttribute("aria-expanded", "true");
					button?.classList.remove("collapsed");
				});
			});
			await page.evaluate(() => { window.__contractNoReload = true; });
			// The toggle cycles system, light, dark; click until the wanted preference is active.
			const choose = async (want) => {
				for (let n = 0; n < 3; n += 1) {
					if (await page.evaluate(() => document.documentElement.getAttribute("data-user-theme")) === want) break;
					await page.locator("#theme-toggle").click();
				}
				await page.waitForFunction((x) => document.documentElement.getAttribute("data-user-theme") === x, want);
				await settled(page);
			};
			await choose("light");
			const light = await snapshot(page);
			await choose("dark");
			const dark = await snapshot(page);
			assert.equal(light.theme, "light");
			assert.equal(dark.theme, "dark");
			assert.ok(light.marker && dark.marker, "the theme switch must not reload the page");
			assert.notEqual(dark.bodyBg, light.bodyBg, "page background did not change");
			assert.notEqual(dark.cardBg, light.cardBg, "card background did not change");
			assert.equal(light.wordmarkInverted, false);
			assert.equal(dark.wordmarkInverted, true, "the wordmark treatment follows the theme");
			assert.ok(light.rects.length > 5, "the page shows visible components");
			assert.deepEqual(dark.rects, light.rects, "a theme switch moves or resizes components");
			// DC-THEME-1: system mode tracks the operating-system preference.
			await choose("system");
			await page.emulateMedia({ colorScheme: "dark" });
			await page.waitForFunction(() => document.documentElement.getAttribute("data-bs-theme") === "dark");
			await page.emulateMedia({ colorScheme: "light" });
			await page.waitForFunction(() => document.documentElement.getAttribute("data-bs-theme") === "light");
		} finally {
			await context.close();
		}
	});
}

appTest("forced colors give status dots and the active tab system colours @contract-invariants", async ({ browser, app, adminState, browserName }) => {
	const context = await browser.newContext({ viewport: { width: 1280, height: 720 }, storageState: adminState, forcedColors: "active" });
	try {
		const page = await context.newPage();
		await page.goto(`${app.base}/ui/settings`, { waitUntil: "load" });
		await page.waitForSelector("#settingsTabs .nav-link.active");
		const active = await page.evaluate(() => window.matchMedia("(forced-colors: active)").matches);
		appTest.skip(!active, `forced colors emulation is not active in ${browserName}`);
		const result = await page.evaluate(() => {
			const system = (property, value) => { const probe = document.createElement("i"); probe.style[property] = value; document.body.append(probe); const out = getComputedStyle(probe)[property]; probe.remove(); return out; };
			const tab = getComputedStyle(document.querySelector("#settingsTabs .nav-link.active"));
			return { tabBorder: tab.borderTopColor, tabLine: tab.textDecorationLine, highlight: system("borderTopColor", "Highlight") };
		});
		assert.ok(result.tabLine.includes("underline"), "the active tab keeps an underline in forced colors");
		assert.equal(result.tabBorder, result.highlight, "the active tab border uses Highlight");
		await page.goto(`${app.base}/`, { waitUntil: "load" });
		// The queue status dot is rendered once its metrics arrived (state signal, not a delay).
		await page.waitForSelector('[data-widget-id="delivery-queue"] .ju-status-dot:not(.ju-status-dot--unknown)', { timeout: 15000 });
		const dots = await page.evaluate(() => [...document.querySelectorAll(".ju-status-dot:not(.ju-status-dot--unknown)")].slice(0, 12).map((el) => {
			const probe = document.createElement("i"); probe.style.color = "CanvasText"; document.body.append(probe); const text = getComputedStyle(probe).color; probe.remove();
			return { background: getComputedStyle(el).backgroundColor, text };
		}));
		assert.ok(dots.length > 0, "the dashboard shows status dots");
		for (const dot of dots) assert.equal(dot.background, dot.text, "a status dot uses CanvasText");
	} finally {
		await context.close();
	}
});

// DC-TOK-6: the shared reduced-motion rule caps every transition and animation. A no-preference control proves the
// measurement can see motion, so a page without any motion cannot pass vacuously.
const MOTION = () => {
	const ms = (value) => Math.max(...value.split(",").map((part) => (part.trim().endsWith("ms") ? parseFloat(part) : parseFloat(part) * 1000)));
	return [...document.querySelectorAll("body, body *")].map((el) => {
		const cs = getComputedStyle(el);
		return { longest: Math.max(ms(cs.transitionDuration), ms(cs.animationDuration)), name: `${el.tagName.toLowerCase()}.${String(el.className).split(" ")[0]}` };
	}).filter((entry) => entry.longest > 0.011);
};
for (const route of ["/ui/targets", "/ui/settings", "/ui/notifications", "/ui/targets/new", "/ui/targets/1/edit"]) {
	appTest(`reduced motion caps transitions and animations on ${route} @contract-invariants`, async ({ browser, app, adminState }) => {
		const context = await browser.newContext({ viewport: { width: 1280, height: 720 }, storageState: adminState });
		try {
			const page = await context.newPage();
			await page.goto(`${app.base}${route}`, { waitUntil: "load" });
			await page.waitForSelector("main");
			await page.emulateMedia({ reducedMotion: "no-preference" });
			assert.ok((await page.evaluate(MOTION)).length > 0, "control: the page defines motion without the preference");
			await page.emulateMedia({ reducedMotion: "reduce" });
			await page.waitForFunction(() => window.matchMedia("(prefers-reduced-motion: reduce)").matches);
			const exceeding = await page.evaluate(MOTION);
			assert.deepEqual(exceeding.slice(0, 10), [], "motion longer than 0.01 ms under prefers-reduced-motion: reduce");
		} finally {
			await context.close();
		}
	});
}
appTest("reduced motion caps transitions and animations on /ui/login @contract-invariants", async ({ browser, app }) => {
	const context = await browser.newContext({ viewport: { width: 1280, height: 720 } });
	try {
		const page = await context.newPage();
		await page.goto(`${app.base}/ui/login`, { waitUntil: "load" });
		await page.waitForSelector("#login-form");
		await page.emulateMedia({ reducedMotion: "no-preference" });
		assert.ok((await page.evaluate(MOTION)).length > 0, "control: the page defines motion without the preference");
		await page.emulateMedia({ reducedMotion: "reduce" });
		await page.waitForFunction(() => window.matchMedia("(prefers-reduced-motion: reduce)").matches);
		assert.deepEqual((await page.evaluate(MOTION)).slice(0, 10), [], "motion longer than 0.01 ms under prefers-reduced-motion: reduce");
	} finally {
		await context.close();
	}
});
// ---- DC-CARD-2: disclosures exist only where the contract names them ----
// A disclosure is a native <details> or a button with aria-expanded. Dropdown, tab and menu triggers are no disclosures.
// Not covered here: the traceroute hop-group toggles (they exist only in the map fragment, tests/layout/traceroute-map.spec.mjs) and the markdown
// of the About changelog (content, not a component). `+N more` (DC-TLIST-2) is named by the target list contract.
const DISCLOSURE_PAGES = [
	{ id: "targets", path: "/ui/targets" }, { id: "wizard", path: "/ui/targets/new" }, { id: "target-edit", path: "/ui/targets/1/edit" },
	{ id: "target-detail", path: "/ui/targets/1" }, { id: "about", path: "/ui/about" }, { id: "groups", path: "/ui/groups" },
	{ id: "dashboard", path: "/" }, { id: "notifications", path: "/ui/notifications" },
];
const SETTINGS_TABS = ["general", "network", "storage", "backup", "smtp", "users", "recipients", "signal", "spooler"];
const listDisclosures = (page) => page.evaluate(() => {
	const allowed = [
		["target group", (el) => el.matches(".targets-group-header")],
		["+N more check list (DC-TLIST-2)", (el) => el.matches(".targets-check-toggle")],
		["Nmap disclosure of the port picker", (el) => el.matches("details.ju-port-discover, details.ju-port-other")],
		["HTTP option section", (el) => el.matches("#http-options-accordion .accordion-button")],
		["Advanced settings of the wizard", (el) => el.matches("details#wizard-advanced")],
		["custom date range", (el) => el.matches("details#target-custom-range")],
		["available placeholders", (el) => el.matches("button[aria-controls='placeholdersCollapse']")],
		["Signal Device Actions", (el) => el.matches("#signal-setup-collapse-toggle")],
		["Signal message templates", (el) => el.matches("#signalTemplatesAccordion .accordion-button")],
		["Signal voice 'More options'", (el) => el.matches("details") && el.querySelector(":scope > summary")?.textContent.trim() === "More options"],
	];
	const skip = "[data-bs-toggle=dropdown], [data-bs-toggle=tab], [data-bs-toggle=pill], [role=tab], [role=combobox], [role=menuitem], [aria-haspopup]";
	return [...document.querySelectorAll("main details, main [aria-expanded]")]
		.filter((el) => !el.matches(skip) && !el.closest(".dropdown-menu, .modal, .changelog-content"))
		.map((el) => {
			const name = allowed.find(([, test]) => test(el))?.[0] ?? null;
			let pattern = true;
			if (el.matches("details")) pattern = el.firstElementChild?.tagName === "SUMMARY";
			else {
				const target = el.getAttribute("aria-controls") || (el.getAttribute("data-bs-target") || "").replace(/^#/, "");
				pattern = ["true", "false"].includes(el.getAttribute("aria-expanded")) && Boolean(target && document.getElementById(target));
			}
			return { node: `${el.tagName.toLowerCase()}${el.id ? `#${el.id}` : ""}.${String(el.className).split(" ").filter(Boolean).slice(0, 3).join(".")}`, text: (el.textContent || "").trim().replace(/\s+/g, " ").slice(0, 40), name, pattern };
		});
});
appTest("disclosures inside cards are the ones DC-CARD-2 names and follow the disclosure pattern @contract-invariants", async ({ browser, app, adminState }) => {
	const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, storageState: adminState });
	try {
		const page = await context.newPage();
		const found = new Map();
		const record = (entries) => entries.forEach((entry) => found.set(`${entry.node}|${entry.text}`, entry));
		for (const entry of DISCLOSURE_PAGES) {
			await page.goto(`${app.base}${entry.path}`, { waitUntil: "load" });
			await page.waitForSelector("main");
			record(await listDisclosures(page));
		}
		await page.goto(`${app.base}/ui/settings`, { waitUntil: "load" });
		for (const tab of SETTINGS_TABS) {
			await page.click(`#${tab}-tab`);
			await page.waitForFunction((id) => { const pane = document.getElementById(`${id}-pane`); return pane.classList.contains("active") && pane.classList.contains("show") && !pane.querySelector(":scope > .text-center .spinner-border"); }, tab); // the placeholder is replaced by the fragment
			record(await listDisclosures(page));
		}
		const entries = [...found.values()];
		assert.deepEqual(entries.filter((entry) => !entry.name).map((entry) => `${entry.node} "${entry.text}"`), [], "a disclosure the contract does not name (DC-CARD-2: add it to the list in the same change)");
		assert.deepEqual(entries.filter((entry) => !entry.pattern).map((entry) => `${entry.node} "${entry.text}"`), [], "a disclosure that is no <details>/<summary> and no aria-expanded button with an existing target");
		// Every named kind that lives on a page of this run was found at least once, so the scan cannot pass vacuously.
		const kinds = new Set(entries.map((entry) => entry.name));
		for (const kind of ["target group", "Nmap disclosure of the port picker", "HTTP option section", "Advanced settings of the wizard", "custom date range", "available placeholders", "Signal Device Actions", "Signal message templates", "Signal voice 'More options'"]) assert.ok(kinds.has(kind), `no "${kind}" disclosure was found: the scan missed its page`);
	} finally {
		await context.close();
	}
});
// ---- DC-SCROLL-5: the registered vertical scroll regions on the real pages ----
// The registry is the one of the shell audit (harness CFG.scrollY). A region without focusable content is keyboard-scrollable
// (tabindex="0") and named, and its height comes from the layout chain or a max-height, never from a fixed `height`.
const fixedHeightDeclarations = (selectors) => {
	const fixed = /^-?\d*\.?\d+(px|rem|em|vh|dvh|svh|lvh|vmin|vmax|cm|mm|in|pt|pc)$/;
	const found = [];
	const walk = (rules, sheet) => {
		for (const rule of rules) {
			if (rule.cssRules && rule.media && !window.matchMedia(rule.media.mediaText).matches) continue;
			if (rule.cssRules && rule.conditionText && rule.constructor.name === "CSSContainerRule") continue; // not evaluated: the width of the container is unknown here
			if (rule.cssRules) { walk(rule.cssRules, sheet); continue; }
			if (!rule.selectorText || !rule.style) continue;
			const height = rule.style.getPropertyValue("height").trim();
			if (!height || !fixed.test(height) || height.startsWith("0")) continue;
			for (const element of document.querySelectorAll(selectors)) {
				let hit = false;
				try { hit = element.matches(rule.selectorText); } catch { /* selector the engine does not know */ }
				if (hit) found.push(`${rule.selectorText} { height: ${height} }`);
			}
		}
	};
	for (const sheet of document.styleSheets) {
		try { walk(sheet.cssRules, sheet); } catch { /* a cross-origin sheet */ }
	}
	return [...new Set(found)];
};
const SCROLL_PAGES = [
	{ id: "about", path: "/ui/about" }, { id: "wizard", path: "/ui/targets/new" }, { id: "target-edit", path: "/ui/targets/1/edit" },
	{ id: "target-detail", path: "/ui/targets/1" }, { id: "dashboard", path: "/" }, { id: "notifications", path: "/ui/notifications" },
];
const inspectRegions = (page, selectors) => page.evaluate(({ selectorList, source }) => {
	const FOCUSABLE = "a[href], button:not([disabled]), input:not([disabled]):not([type=hidden]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex='-1'])";
	const fixedHeights = new Function(`return (${source})`)();
	const regions = [...document.querySelectorAll(selectorList)].filter((el) => el.getClientRects().length && getComputedStyle(el).visibility !== "hidden");
	return {
		count: regions.length,
		keyboard: regions.filter((el) => !el.querySelector(FOCUSABLE) && !(el.getAttribute("tabindex") === "0" && (el.hasAttribute("aria-label") || el.hasAttribute("aria-labelledby")))).map((el) => `${el.tagName.toLowerCase()}.${String(el.className).split(" ")[0]}`),
		fixed: fixedHeights(selectorList),
		inline: regions.filter((el) => /(^|;)\s*height\s*:/.test(el.getAttribute("style") || "")).map((el) => `${el.tagName.toLowerCase()}.${String(el.className).split(" ")[0]}`),
	};
}, { selectorList: selectors, source: fixedHeightDeclarations.toString() });
appTest("registered scroll regions are keyboard-scrollable, named and not fixed in height @contract-invariants", async ({ browser, app, adminState }) => {
	const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, storageState: adminState });
	try {
		const page = await context.newPage();
		const selectors = CFG.scrollY.filter((selector) => selector !== "#server-offline-overlay").join(", ");
		let regions = 0;
		const keyboard = []; const fixed = []; const inline = [];
		const scan = async (label) => {
			const result = await inspectRegions(page, selectors);
			regions += result.count;
			keyboard.push(...result.keyboard.map((node) => `${label}: ${node}`));
			fixed.push(...result.fixed.map((rule) => `${label}: ${rule}`));
			inline.push(...result.inline.map((node) => `${label}: ${node}`));
		};
		for (const entry of SCROLL_PAGES) {
			await page.goto(`${app.base}${entry.path}`, { waitUntil: "load" });
			await page.waitForSelector("main");
			await scan(entry.id);
		}
		await page.goto(`${app.base}/ui/settings`, { waitUntil: "load" });
		for (const tab of SETTINGS_TABS) {
			await page.click(`#${tab}-tab`);
			await page.waitForFunction((id) => { const pane = document.getElementById(`${id}-pane`); return pane.classList.contains("active") && pane.classList.contains("show") && !pane.querySelector(":scope > .text-center .spinner-border"); }, tab);
			await scan(`settings/${tab}`);
		}
		assert.ok(regions >= 6, `registered regions found on the real pages: ${regions}`);
		assert.deepEqual(keyboard, [], "a scroll region without focusable content that has no tabindex=0 and accessible name");
		assert.deepEqual(inline, [], "a scroll region with an inline height");
		assert.deepEqual(fixed, [], "a scroll region whose height is a fixed length in a style rule");
		console.log(`[scroll-5] ${regions} registered regions inspected`);
	} finally {
		await context.close();
	}
});
// ---- DC-FORM-14: hints, counts and conflict lists are polite; only failures are alerts; loading and the reserved result line ----
const FAILURE_REGION = ".ju-modal-error, #wizard-error, .alert-danger, [data-scan-error], [data-service-error], #downtime-error, #global-toast, .toast";
const POLITE_REGIONS = [
	{ page: "/ui/targets", selector: "#targets-search-status", what: "search result count" },
	{ page: "/ui/targets/new", selector: "#wizard-conflicts", what: "conflict list" },
	{ page: "/ui/targets/new", selector: "#wizard-summary", what: "summary" },
	{ page: "/ui/targets/new", selector: "[data-scan-status]", what: "port scan status" },
	{ page: "/ui/settings", selector: "#recipients-search-status", what: "recipient search count" },
];
appTest("hints, counts and conflict lists use polite regions; only failure regions are alerts @contract-invariants", async ({ browser, app, adminState }) => {
	const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, storageState: adminState });
	try {
		const page = await context.newPage();
		const alerts = []; const loading = []; let reserved = null;
		const scan = async (label) => {
			const result = await page.evaluate((failureSelector) => ({
				alerts: [...document.querySelectorAll("[role=alert], [aria-live=assertive]")].filter((el) => !el.matches(failureSelector)).map((el) => `${el.tagName.toLowerCase()}#${el.id}.${String(el.className).split(" ")[0]}`),
				// Loading is a word or a named status next to the spinner, never the spinner alone.
				loading: [...document.querySelectorAll("main .spinner-border")].filter((el) => el.getClientRects().length).filter((el) => !(el.getAttribute("aria-label") || el.querySelector(".visually-hidden")?.textContent.trim() || el.closest("[aria-label]")?.getAttribute("aria-label") || el.parentElement.textContent.trim())).map((el) => `${el.parentElement.tagName.toLowerCase()}.${String(el.parentElement.className).split(" ")[0]}`),
				reserved: [...document.querySelectorAll(".ju-test-result")].map((el) => ({ min: parseFloat(getComputedStyle(el).minHeight), line: parseFloat(getComputedStyle(el).fontSize) * 1.2, role: el.getAttribute("role"), live: el.getAttribute("aria-live") })),
			}), FAILURE_REGION);
			alerts.push(...result.alerts.map((node) => `${label}: ${node}`));
			loading.push(...result.loading.map((node) => `${label}: ${node}`));
			if (result.reserved.length) reserved = result.reserved;
		};
		for (const route of ["/ui/targets", "/ui/targets/new", "/ui/targets/1/edit", "/ui/targets/1", "/", "/ui/notifications"]) {
			await page.goto(`${app.base}${route}`, { waitUntil: "load" });
			await page.waitForSelector("main");
			await scan(route);
			for (const region of POLITE_REGIONS.filter((entry) => entry.page === route)) {
				const state = await page.locator(region.selector).first().evaluate((el) => ({ live: el.getAttribute("aria-live") ?? el.closest("[aria-live]")?.getAttribute("aria-live"), role: el.getAttribute("role"), inner: el.querySelectorAll("[role=alert], [aria-live=assertive]").length }));
				assert.ok(state.live === "polite" || state.role === "status", `${region.what} (${region.selector}) is a polite region: ${JSON.stringify(state)}`);
				assert.equal(state.role === "alert" || state.live === "assertive", false, `${region.what} is no alert`);
				assert.equal(state.inner, 0, `${region.what} holds no alert`);
			}
		}
		await page.goto(`${app.base}/ui/settings`, { waitUntil: "load" });
		for (const tab of SETTINGS_TABS) {
			await page.click(`#${tab}-tab`);
			await page.waitForFunction((id) => { const pane = document.getElementById(`${id}-pane`); return pane.classList.contains("active") && pane.classList.contains("show") && !pane.querySelector(":scope > .text-center .spinner-border"); }, tab);
			await scan(`settings/${tab}`);
		}
		const state = await page.locator("#recipients-search-status").first().evaluate((el) => ({ live: el.getAttribute("aria-live"), role: el.getAttribute("role") }));
		assert.ok(state.live === "polite" && state.role === "status", "recipient search count is a polite status region");
		assert.deepEqual(alerts, [], "role=alert / aria-live=assertive on something that is not a failure region (DC-FORM-14)");
		assert.deepEqual(loading, [], "a spinner without a word or accessible name (loading must not rely on the spinner alone)");
		assert.ok(reserved?.length, "the settings page carries the test-result line");
		for (const line of reserved) {
			assert.equal(line.role, "status", "the test result is a status region");
			assert.equal(line.live, "polite", "the test result is polite");
			assert.ok(line.min >= line.line, `the test result reserves one line (${line.min}px of ${Math.round(line.line)}px)`);
		}
		// A count announced through the polite region: typing a search updates the text, no alert is created.
		await page.goto(`${app.base}/ui/targets`, { waitUntil: "load" });
		await page.waitForSelector("#targets-search-status", { state: "attached" });
		await page.locator("input[type=search]").first().fill("zzz-no-such-target");
		await page.waitForFunction(() => document.getElementById("targets-search-status").textContent.trim().length > 0);
		assert.equal(await page.locator("[role=alert]:not(.toast)").evaluateAll((els) => els.filter((el) => el.textContent.trim()).length), 0, "searching raises no alert");
	} finally {
		await context.close();
	}
});
// ---- T-27 / OR-17 / DC-TOK-5: elevation tokens as the browser draws them, in both themes ----
// Cards that carry Bootstrap's `shadow-sm` utility (30 templates, e.g. the Settings cards) are mapped to --ju-shadow in ui.css.
for (const theme of ["light", "dark"]) {
	appTest(`cards and dialogs draw the shared elevation tokens (${theme}) @contract-invariants`, async ({ browser, app, adminState }) => {
		const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, storageState: adminState });
		try {
			const page = await context.newPage();
			const token = (name) => page.evaluate((x) => { const probe = document.createElement("i"); probe.style.boxShadow = `var(${x})`; document.body.append(probe); const value = getComputedStyle(probe).boxShadow; probe.remove(); return value; }, name);
			for (const route of ["/ui/targets", "/ui/about", "/ui/settings"]) {
				await page.goto(`${app.base}${route}`, { waitUntil: "load" });
				await page.evaluate((mode) => document.documentElement.setAttribute("data-bs-theme", mode), theme);
				const card = await token("--ju-shadow");
				const drawn = await page.evaluate(() => [...document.querySelectorAll("main .card")].map((el) => getComputedStyle(el).boxShadow));
				assert.ok(drawn.length > 0, `${route}: cards with the shared frame found`);
				assert.deepEqual([...new Set(drawn)], [card], `${route}: a card does not draw --ju-shadow`);
			}
			await page.goto(`${app.base}/`, { waitUntil: "load" });
			await page.evaluate((mode) => document.documentElement.setAttribute("data-bs-theme", mode), theme);
			const [raised, subtle, modalAlias] = [await token("--ju-shadow-raised"), await token("--ui-shadow-subtle"), await token("--ui-modal-shadow")];
			assert.equal(modalAlias, raised, "--ui-modal-shadow is the alias of --ju-shadow-raised in this theme");
			assert.notEqual(raised, subtle, "the floating level differs from the inline level");
			await page.evaluate(() => new Promise((resolve) => {
				const modal = document.getElementById("dashboard-widgets-modal");
				modal.addEventListener("shown.bs.modal", resolve, { once: true });
				window.bootstrap.Modal.getOrCreateInstance(modal).show();
			}));
			assert.equal(await page.evaluate(() => getComputedStyle(document.querySelector("#dashboard-widgets-modal .modal-content")).boxShadow), raised, "a dialog draws --ju-shadow-raised");
		} finally {
			await context.close();
		}
	});
}
