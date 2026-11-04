//
// tests/layout/targets-search.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// Targets page search: filters while typing, opens groups with hits for the duration of the search,
// restores the manual state afterwards and survives fragment polling (DESIGN.md,
// "Filter-as-you-type search"). Seeded targets: Production (1) and Websites (3).
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { test } from "./fixtures.mjs";
import { waitForVisualStability } from "./harness.mjs";

const VIEWPORTS = [
	{ name: "1280x720", width: 1280, height: 720, dsf: 1 },
	{ name: "390x844", width: 390, height: 844, dsf: 1 },
	{ name: "320x640", width: 320, height: 640, dsf: 1 },
	{ name: "1280x720@200%", width: 640, height: 360, dsf: 2 },
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
				assert.deepEqual(await visibleGroups(page).then((list) => list.map((group) => group.name)), ["Websites"], `${theme}: groups without a hit disappear`);
				const hit = (await visibleGroups(page))[0];
				assert.equal(hit.expanded && hit.shown, true, `${theme}: the group with a hit opens for the search`);
				assert.equal(hit.visibleRows, 1, `${theme}: only the matching row stays`);
				assert.equal(hit.badge, "1 / 3", `${theme}: the badge shows hits against the total`);
				assert.equal(await status.textContent(), "1 target found", `${theme}: live region announces the count`);
				assert.equal(await clear.isVisible(), true, `${theme}: clear button appears with a query`);
				assert.equal(await storedGroupState(page), stored, `${theme}: opening a group for the search does not overwrite the stored state`);
				assert.equal(await empty.isHidden(), true);
				if (SHOTS && theme) {
					fs.mkdirSync(SHOTS, { recursive: true });
					await page.screenshot({ path: path.join(SHOTS, `targets-search-hits-${theme}-${viewport.name.replace(/[@%]/g, "_")}.png`) });
				}

				// Escape clears, restores the manual state and keeps focus in the field.
				await page.keyboard.press("Escape");
				assert.equal(await search.inputValue(), "", `${theme}: Escape clears the field`);
				assert.equal(await page.evaluate(() => document.activeElement.id), "targets-search", `${theme}: focus stays in the field`);
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
			if (viewport.width <= 390) assert.ok(geometry.fieldWidth >= geometry.viewport - 64, `the field takes the full width on a phone (${geometry.fieldWidth}px)`);
			assert.ok(geometry.fieldHeight >= 24 && geometry.clearW >= 24 && geometry.clearH >= 24, "touch targets are at least 24x24 CSS px");
			assert.ok(geometry.clearInside, "the clear button sits inside the field");

			// Keyboard: the field follows the page actions in tab order and shows a visible focus ring.
			await page.locator('a[href="/ui/targets/new"]').focus();
			await page.keyboard.press("Tab");
			const focus = await page.evaluate(() => {
				const style = getComputedStyle(document.activeElement);
				return { id: document.activeElement.id, outline: style.outlineStyle, width: parseFloat(style.outlineWidth), shadow: style.boxShadow };
			});
			assert.equal(focus.id, "targets-search", "Tab from New Target reaches the search field");
			assert.ok((focus.outline !== "none" && focus.width >= 2) || focus.shadow !== "none", `visible keyboard focus (${focus.outline} ${focus.width}px ${focus.shadow})`);
			assert.deepEqual(problems, [], "no console or page errors");
		} finally {
			await context.close();
		}
	});
}

// Edit and delete stay reachable on a filtered row.
async function expect_row_actions(page) {
	const row = page.locator("#targets-table tbody tr[data-search]:not([hidden])").first();
	assert.equal(await row.getByRole("link", { name: "Edit target" }).count(), 1);
	assert.equal(await row.getByRole("button", { name: "Delete" }).count(), 1);
	assert.equal(await row.getByRole("button", { name: "Delete" }).isEnabled(), true);
}
