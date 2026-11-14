//
// tests/layout/targets-search.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Targets page search: filters while typing, opens groups with hits for the duration of the search,
// restores the manual state afterwards and survives fragment polling (DESIGN.md DC-SEARCH-*). Seeded targets: Production (1) and Websites (3).
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { expect } from "@playwright/test";
import { contrastRatio, test } from "./fixtures.mjs";
import { waitForVisualStability } from "./harness.mjs";
import { applyTextEnlargement, cssViewport, effectiveZoom, textEnlargement } from "./test-support.mjs";

// Four distinct notions (DESIGN.md 1.2, DC-MEAS-1; helpers in test-support.mjs):
//  - css-320x640 is the WCAG reflow equivalent of 400 % zoom (a CSS viewport 320 px wide);
//  - 1280x720-effective-zoom-200pct is 640x360 CSS px at DPR 2: a viewport proxy, not the browser zoom feature;
//  - css-1280x720-text-200pct and css-390x844-text-200pct enlarge the root font size at an unchanged CSS viewport;
//  - DPR alone proves no reflow and has no profile of its own.
const VIEWPORTS = [
	cssViewport(1280, 720),
	cssViewport(390, 844),
	cssViewport(320, 640),
	effectiveZoom(1280, 720, 2),
	textEnlargement(1280, 720, 2),
	textEnlargement(390, 844, 2),
];
const THEMES = ["light", "dark"];
const SHOTS = process.env.JUSTUP_TARGETS_SEARCH_SHOTS;

async function openTargets(browser, app, adminState, viewport) {
	const context = await browser.newContext({
		viewport: { width: viewport.width, height: viewport.height }, deviceScaleFactor: viewport.dsf, storageState: adminState,
	});
	const page = await context.newPage();
	const problems = [];
	page.on("pageerror", (error) => problems.push(String(error)));
	page.on("console", (message) => { if (message.type() === "error") problems.push(message.text()); });
	await page.goto(app.base + "/ui/targets", { waitUntil: "load" });
	await applyTextEnlargement(page, viewport.textScale);
	await page.waitForFunction(() => document.querySelectorAll("#targets-table tbody tr[data-search]").length >= 4, null, { timeout: 20000 });
	// Start from the documented default: every group open (a narrow viewport collapses them on load).
	await page.evaluate(() => {
		localStorage.clear();
		document.querySelectorAll("#targets-table .accordion-collapse").forEach((collapse) => {
			collapse.classList.add("show");
			document.querySelector(`[data-bs-target="#${collapse.id}"]`).setAttribute("aria-expanded", "true");
			document.querySelector(`[data-bs-target="#${collapse.id}"]`).classList.remove("collapsed");
		});
	});
	return { context, page, problems };
}

// DESIGN.md DC-FOCUS-1 variant V1: a text field shows focus by its border alone (no outline, no shadow).
// Bootstrap animates .form-control border-color (0.15s); on a slow runner two frames end mid-transition.
// Waits until the field has no running animation or transition, then checks the real focus owner.
async function expectFieldFocus(page, label) {
	await page.waitForFunction(() => document.getElementById("targets-search").getAnimations().length === 0);
	await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
	const state = await page.evaluate(() => {
		const el = document.getElementById("targets-search");
		const cs = getComputedStyle(el);
		const probe = document.createElement("i");
		probe.style.cssText = "position:absolute;visibility:hidden;border:1px solid var(--ju-focus-field-border)";
		document.body.append(probe);
		const token = getComputedStyle(probe).borderTopColor;
		probe.remove();
		return {
			active: document.activeElement === el, focusVisible: el.matches(":focus-visible"), border: cs.borderTopColor, token,
			outline: cs.outlineStyle, shadow: cs.boxShadow, background: cs.backgroundColor,
		};
	});
	assert.equal(state.active, true, `${label}: the search field owns the focus`);
	assert.equal(state.focusVisible, true, `${label}: the focus is keyboard-visible`);
	assert.equal(state.border, state.token, `${label}: focus shows as the field border (${state.border} vs ${state.token})`);
	assert.ok(state.outline === "none" && state.shadow === "none", `${label}: no outline or glow (${state.outline} ${state.shadow})`);
	const contrast = contrastRatio(state.border, state.background);
	assert.ok(contrast >= 3, `${label}: focus border contrast ${contrast.toFixed(2)}:1 is below 3:1`);
}

const setTheme = async (page, theme) => {
	await page.evaluate((mode) => document.documentElement.setAttribute("data-bs-theme", mode), theme);
	await waitForVisualStability(page);
};

// One entry per group: name, open state (aria-expanded and .show must agree), visibility and badge.
const groups = (page) => page.evaluate(() => Array.from(document.querySelectorAll("#targets-table .accordion-item")).map((item) => {
	const button = item.querySelector(".accordion-button");
	const collapse = item.querySelector(".accordion-collapse");
	return {
		name: button.querySelector("strong").textContent.trim(),
		badge: item.querySelector(".targets-group-count").textContent.trim(),
		expanded: button.getAttribute("aria-expanded") === "true",
		shown: collapse.classList.contains("show"),
		hidden: item.hidden,
		visibleRows: Array.from(item.querySelectorAll("tr[data-search]")).filter((row) => !row.hidden).length,
	};
}));
const visibleGroups = async (page) => (await groups(page)).filter((group) => !group.hidden);
const storedGroupState = (page) => page.evaluate(() => JSON.stringify(Object.entries(localStorage).filter(([key]) => key.startsWith("targets_group_")).sort()));
const api = (page, url, method, body) => page.evaluate(async ([target, verb, payload]) => {
	const response = await apiFetch(target, { method: verb, headers: { "Content-Type": "application/json" }, body: payload ? JSON.stringify(payload) : undefined });
	return { status: response.status, body: verb === "DELETE" ? null : await response.json() };
}, [url, method, body]);

for (const viewport of VIEWPORTS) {
	test(`targets search filters, restores groups and survives polling at ${viewport.name} @targets-search`, async ({ browser, app, adminState }) => {
		const { context, page, problems } = await openTargets(browser, app, adminState, viewport);
		const search = page.getByRole("searchbox", { name: "Search targets" });
		const status = page.locator("#targets-search-status");
		const clear = page.locator("#targets-search-clear");
		const empty = page.locator("#targets-search-empty");
		try {
			for (const theme of THEMES) {
				await setTheme(page, theme);
				await search.fill("");
				await page.locator("#targets-search-clear").evaluate((button) => { button.hidden = true; });
				const before = await groups(page);
				assert.equal(before.length, 2, `${theme}: Production and Websites render as groups`);
				assert.ok(before.every((group) => group.expanded && group.shown), `${theme}: groups start open`);
				assert.equal(await clear.isHidden(), true, `${theme}: no clear button without a query`);
				assert.equal(await status.textContent(), "", `${theme}: no announcement without a query`);

				// The user closes Websites by hand: that is the state to return to.
				await page.locator('.accordion-button[data-bs-target^="#group-websites"]').click();
				await page.waitForFunction(() => !document.querySelector('[id^="group-websites"]').classList.contains("show") &&
					!document.querySelector('[id^="group-websites"]').classList.contains("collapsing"));
				const manual = await groups(page);
				const stored = await storedGroupState(page);
				assert.equal(manual.find((group) => group.name === "Websites").expanded, false, `${theme}: manual close took effect`);

				// Typing filters at once, folds diacritics and matches a substring of the name.
				await search.focus();
				await page.keyboard.type("LUETT", { delay: 10 });
				// The filter settles after the last keystroke; the live region is its completion signal.
				await expect.poll(() => status.textContent(), { message: `${theme}: filter applied` }).toBe("1 target found");
				assert.deepEqual(await visibleGroups(page).then((list) => list.map((group) => group.name)), ["Websites"], `${theme}: groups without a hit disappear`);
				const hit = (await visibleGroups(page))[0];
				assert.equal(hit.expanded && hit.shown, true, `${theme}: the group with a hit opens for the search`);
				assert.equal(hit.visibleRows, 1, `${theme}: only the matching row stays`);
				assert.equal(hit.badge, "1 / 3", `${theme}: the badge shows hits against the total`);
				assert.equal(await status.textContent(), "1 target found", `${theme}: live region announces the count`);
				assert.equal(await clear.isVisible(), true, `${theme}: clear button appears with a query`);
				assert.equal(await storedGroupState(page), stored, `${theme}: opening a group for the search does not overwrite the stored state`);
				assert.equal(await empty.isHidden(), true);
				if (SHOTS) {
					fs.mkdirSync(SHOTS, { recursive: true });
					await page.screenshot({ path: path.join(SHOTS, `targets-search-hits-${theme}-${viewport.name}.png`) });
				}

				// Escape clears, restores the manual state and keeps focus in the field.
				await page.keyboard.press("Escape");
				assert.equal(await search.inputValue(), "", `${theme}: Escape clears the field`);
				assert.equal(await page.evaluate(() => document.activeElement.id), "targets-search", `${theme}: focus stays in the field`);
				await expectFieldFocus(page, `${theme} after Escape`);
				assert.deepEqual(await groups(page), manual.map((group) => ({ ...group, badge: group.badge })), `${theme}: groups, badges and rows return to the state before the search`);
				assert.equal(await clear.isHidden(), true);
				assert.equal(await status.textContent(), "");

				// Host, URL and group name are searched as well; regular-expression characters are literal.
				await search.fill("example.net");
				assert.equal((await visibleGroups(page))[0].visibleRows, 1, `${theme}: the host is searched`);
				await search.fill("production");
				assert.deepEqual((await visibleGroups(page)).map((group) => [group.name, group.visibleRows]), [["Production", 1]], `${theme}: the group name is searched`);
				await search.fill("websites");
				assert.equal((await visibleGroups(page))[0].badge, "3 / 3", `${theme}: a group-name hit keeps all its targets`);
				await search.fill("  EXAMPLE.ORG ");
				assert.equal((await visibleGroups(page)).reduce((sum, group) => sum + group.visibleRows, 0), 1, `${theme}: trim and case do not matter`);

				for (const query of ["(", ".*", "[a-"]) {
					await search.fill(query);
					assert.equal(await empty.isVisible(), true, `${theme}: '${query}' is no pattern, nothing matches`);
					assert.equal(await page.locator("#targets-search-term").textContent(), `"${query}"`, `${theme}: the term is shown as text`);
					assert.equal(await status.textContent(), "No targets found");
					assert.equal((await visibleGroups(page)).length, 0, `${theme}: no frame stays around nothing`);
				}
				if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `targets-search-empty-${theme}-${viewport.name.replace(/[@%]/g, "_")}.png`) });
				// The empty state offers its own way back.
				await page.getByRole("button", { name: "Show all targets" }).click();
				assert.equal(await search.inputValue(), "");
				assert.equal(await empty.isHidden(), true);
				assert.equal((await visibleGroups(page)).length, 2, `${theme}: clearing shows both groups again`);

				// A toggle by hand during the search becomes the state afterwards.
				await search.fill("luett");
				await page.locator('.accordion-button[data-bs-target^="#group-websites"]').click();
				await page.waitForFunction(() => !document.querySelector('[id^="group-websites"]').classList.contains("show") &&
					!document.querySelector('[id^="group-websites"]').classList.contains("collapsing"));
				await clear.click();
				assert.equal((await groups(page)).find((group) => group.name === "Websites").expanded, false, `${theme}: a manual close during the search survives`);
				// Re-open for the next theme round.
				await page.locator('.accordion-button[data-bs-target^="#group-websites"]').click();
				await page.waitForFunction(() => document.querySelector('[id^="group-websites"]').classList.contains("show") &&
					!document.querySelector('[id^="group-websites"]').classList.contains("collapsing"));
			}

			// Polling replaces the fragment while the query is active: new rows are judged against it.
			await search.fill("luett");
			const created = [];
			try {
				for (const [name, group] of [["luett-polling-new", "Websites"], ["zzz-polling-new", "Websites"]]) {
					const res = await api(page, "/targets", "POST", { name, group, host: `${name}.example.org`, enable_ping: true });
					assert.equal(res.status, 201);
					created.push(res.body.id);
				}
				await page.evaluate(() => window.htmx.trigger(document.getElementById("targets-table"), "refresh"));
				await page.waitForFunction(() => document.querySelectorAll("#targets-table tbody tr[data-search]").length >= 6, null, { timeout: 15000 });
				assert.equal(await search.inputValue(), "luett", "the query survives the fragment swap");
				await search.focus();
				// Arm the listener before the trigger; only a swap of the table itself counts (other polls also settle).
				await page.evaluate(() => {
					window.__tableSettled = new Promise((resolve) => {
						const onSettle = (e) => {
							// htmx 1.9 settles the swapped children, so match the container by ancestry.
							if (!e.target?.closest?.("#targets-table")) return;
							document.body.removeEventListener("htmx:afterSettle", onSettle);
							resolve();
						};
						document.body.addEventListener("htmx:afterSettle", onSettle);
					});
					window.htmx.trigger(document.getElementById("targets-table"), "refresh");
				});
				await page.evaluate(() => window.__tableSettled);
				await expectFieldFocus(page, "after the fragment swap");
				const during = await visibleGroups(page);
				assert.deepEqual(during.map((group) => [group.name, group.visibleRows, group.expanded]), [["Websites", 2, true]], "the new matching row shows, the non-matching one stays hidden");
				// Row actions keep working while a filter is active.
				await expect_row_actions(page);
			} finally {
				for (const id of created) assert.ok([200, 204].includes((await api(page, `/targets/${id}`, "DELETE")).status), "cleanup delete");
			}
			await page.evaluate(() => window.htmx.trigger(document.getElementById("targets-table"), "refresh"));
			await page.waitForFunction(() => {
				if (document.querySelectorAll("#targets-table tbody tr[data-search]").length === 4) return true;
				window.htmx.trigger(document.getElementById("targets-table"), "refresh");
				return false;
			}, null, { timeout: 15000, polling: 1000 });
			assert.equal((await visibleGroups(page))[0].visibleRows, 1, "removed rows leave the result");
			await search.fill("");

			// Geometry: the field uses the width sensibly, targets are large enough, nothing overflows.
			const geometry = await page.evaluate(() => {
				const root = document.documentElement;
				const main = document.querySelector("main");
				const field = document.getElementById("targets-search").getBoundingClientRect();
				const clearBtn = document.getElementById("targets-search-clear");
				const card = document.querySelector(".card.ui-card");
				clearBtn.hidden = false;
				const clearRect = clearBtn.getBoundingClientRect();
				clearBtn.hidden = true;
				const rem = parseFloat(getComputedStyle(root).fontSize);
				return {
					docOverflowX: root.scrollWidth - root.clientWidth, docOverflowY: root.scrollHeight - root.clientHeight,
					mainOverflowX: main.scrollWidth - main.clientWidth, cardOverflowX: card.scrollWidth - card.clientWidth,
					fieldLeft: field.left, fieldRight: field.right, fieldWidth: field.width, fieldHeight: field.height, maxWidth: 28 * rem,
					clearW: clearRect.width, clearH: clearRect.height, clearInside: clearRect.right <= field.right + 0.5 && clearRect.left >= field.left,
					viewport: root.clientWidth,
				};
			});
			assert.ok(geometry.docOverflowX <= 0, `no horizontal page overflow (${geometry.docOverflowX}px)`);
			assert.ok(geometry.docOverflowY <= 1, `the document does not scroll (${geometry.docOverflowY}px)`);
			assert.ok(geometry.mainOverflowX <= 0 && geometry.cardOverflowX <= 0, "no horizontal overflow in main or the card");
			assert.ok(geometry.fieldLeft >= 0 && geometry.fieldRight <= geometry.viewport, "the field stays inside the viewport");
			assert.ok(geometry.fieldWidth <= geometry.maxWidth + 1, "the field has a bounded width on wide pages");
			// Page padding is rem-based and grows with text enlargement, so the allowance scales with it.
			if (viewport.width <= 390) assert.ok(geometry.fieldWidth >= geometry.viewport - 64 * viewport.textScale, `the field takes the full width on a phone (${geometry.fieldWidth}px)`);
			assert.ok(geometry.fieldHeight >= 24 && geometry.clearW >= 24 && geometry.clearH >= 24, "touch targets are at least 24x24 CSS px");
			assert.ok(geometry.clearInside, "the clear button sits inside the field");

			// Keyboard: the field follows the page actions in tab order and shows the quiet field focus.
			await page.locator('a[href="/ui/targets/new"]').focus();
			await page.keyboard.press("Tab");
			assert.equal(await page.evaluate(() => document.activeElement.id), "targets-search", "Tab from New Target reaches the search field");
			await expectFieldFocus(page, "after Tab");
			assert.deepEqual(problems, [], "no console or page errors");
		} finally {
			await context.close();
		}
	});
}

// N48, DC-SEARCH: UDP ports are searchable only while the UDP scan is active (the data-search attribute lists them then).
test("targets search matches UDP ports only for an active UDP scan @targets-search", async ({ browser, app, adminState }) => {
	const { context, page, problems } = await openTargets(browser, app, adminState, cssViewport(1280, 720));
	const search = page.getByRole("searchbox", { name: "Search targets" });
	const status = page.locator("#targets-search-status");
	const created = [];
	try {
		const base = { enable_ping: true, enable_udp_scan: true };
		const active = await api(page, "/targets", "POST", { ...base, name: "udp-search-active", group: "Websites", host: "udp-active.example.org", udp_ports: "40123" });
		assert.equal(active.status, 201);
		created.push(active.body.id);
		const inactive = await api(page, "/targets", "POST", { ...base, name: "udp-search-inactive", group: "Websites", host: "udp-inactive.example.org", udp_ports: "40124" });
		assert.equal(inactive.status, 201);
		created.push(inactive.body.id);
		// The second target keeps its stored UDP ports but switches the scan off.
		const off = await api(page, `/targets/${inactive.body.id}`, "PATCH", { enable_udp_scan: false });
		assert.ok([200, 204].includes(off.status), `scan switched off (${off.status})`);
		await page.waitForFunction(() => {
			if (document.querySelectorAll("#targets-table tbody tr[data-search]").length >= 6) return true;
			window.htmx.trigger(document.getElementById("targets-table"), "refresh");
			return false;
		}, null, { timeout: 15000, polling: 1000 });
		const searchTexts = await page.evaluate(() => Object.fromEntries(Array.from(document.querySelectorAll("#targets-table tbody tr[data-search]")).map((row) => [row.dataset.targetId, row.dataset.search])));
		assert.match(searchTexts[active.body.id], /40123/, "an active UDP scan lists its ports for the search");
		assert.doesNotMatch(searchTexts[inactive.body.id], /40124/, "a disabled UDP scan lists no ports for the search");
		await search.fill("40123");
		await expect.poll(() => status.textContent(), { message: "active UDP port found" }).toBe("1 target found");
		assert.deepEqual((await visibleGroups(page)).map((group) => [group.name, group.visibleRows]), [["Websites", 1]]);
		await search.fill("40124");
		await expect.poll(() => status.textContent(), { message: "inactive UDP port not found" }).toBe("No targets found");
		assert.deepEqual(problems, [], "no console or page errors");
	} finally {
		for (const id of created) assert.ok([200, 204].includes((await api(page, `/targets/${id}`, "DELETE")).status), "cleanup delete");
		await context.close();
	}
});
// Edit and delete stay reachable on a filtered row.
async function expect_row_actions(page) {
	const row = page.locator("#targets-table tbody tr[data-search]:not([hidden])").first();
	assert.equal(await row.getByRole("link", { name: "Edit target" }).count(), 1);
	assert.equal(await row.getByRole("button", { name: "Delete" }).count(), 1);
	assert.equal(await row.getByRole("button", { name: "Delete" }).isEnabled(), true);
}
