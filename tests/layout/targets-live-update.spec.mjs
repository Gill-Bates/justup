//
// tests/layout/targets-live-update.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Targets page live refresh (DESIGN.md, "Load stability"): a poll that changes one status rewrites
// only the changed cell. Unchanged rows, the self-updating status icons, focus, collapsed groups and
// the search filter all stay as they are, and nothing shifts. The poll answer is the real fragment
// with one HTTP badge switched to "down".
import assert from "node:assert/strict";
import { expect } from "@playwright/test";
import { test } from "./fixtures.mjs";
import { waitForVisualStability } from "./harness.mjs";
import { countStateBadges, waitForStateBadgeCount } from "./test-support.mjs";

const CLS_BUDGET = 0.01;

async function open(browser, app, adminState, viewport, { transform = null } = {}) {
	const context = await browser.newContext({ viewport, storageState: adminState });
	const page = await context.newPage();
	const problems = [];
	page.on("pageerror", (error) => problems.push(String(error)));
	await page.addInitScript(() => {
		window.__shifts = 0;
		try { new PerformanceObserver((list) => { for (const entry of list.getEntries()) if (!entry.hadRecentInput) window.__shifts += entry.value; }).observe({ type: "layout-shift", buffered: true }); } catch { /* no layout-shift entries */ }
	});
	let mode = "pass";
	let routeError = null;
	await page.route("**/ui/fragments/targets-table**", async (route) => {
		const response = await route.fetch();
		let body = await response.text();
		if (!response.ok()) routeError = new Error(`the targets fragment answered ${response.status()}: ${body.slice(0, 120)}`);
		if (mode === "changed") {
			// Switch the first HTTP status item that is not yet "down" to down: one cell changes.
			// Markup of the state_dot macro (fragments/_state_dot.html), with an optional title attribute.
			const httpBadge = /<span class="ju-status-item"( title="[^"]*")?><span class="ju-status-dot ju-status-dot--(?:success|neutral) ju-status-dot--(?:up|unknown)" aria-hidden="true"><\/span>HTTP<span class="visually-hidden">: (?:Up|Unknown)<\/span>/;
			if (!httpBadge.test(body)) {
				// An assertion thrown here would vanish in the route handler; surface it from the test instead.
				routeError = new Error("the fixture needs an HTTP status item that is not down yet");
				return route.abort();
			}
			body = body.replace(httpBadge, '<span class="ju-status-item"$1><span class="ju-status-dot ju-status-dot--danger ju-status-dot--down" aria-hidden="true"></span>HTTP<span class="visually-hidden">: Down</span>');
		}
		if (transform) body = await transform(body, new URL(route.request().url()));
		await route.fulfill({ response, body });
	});
	await page.goto(app.base + "/ui/targets", { waitUntil: "load" });
	await page.waitForFunction(() => document.querySelectorAll("#targets-table tbody tr[data-search]").length >= 4, null, { timeout: 20000 });
	await page.waitForFunction(() => !document.querySelector("#targets-table .status-unknown"), null, { timeout: 20000 });
	await waitForVisualStability(page);
	return { context, page, problems, setMode: (value) => { mode = value; }, getRouteError: () => routeError };
}

for (const viewport of [{ width: 1280, height: 720 }, { width: 390, height: 844 }]) {
	test(`a changed poll rewrites only the changed cell at ${viewport.width}x${viewport.height} @targets-live-update`, async ({ browser, app, adminState }) => {
		const { context, page, problems, setMode, getRouteError } = await open(browser, app, adminState, viewport);
		try {
			// Tag the live nodes, then watch the container.
			await page.evaluate(() => {
				const rows = [...document.querySelectorAll("#targets-table tbody tr[data-search]")];
				rows.forEach((row, index) => { row.__tag = index; row.querySelector("td[data-label=Status] > span").__tag = `s${index}`; });
				window.__rows = rows.length;
				window.__removed = 0;
				new MutationObserver((records) => { for (const record of records) { window.__removed += record.removedNodes.length; (window.__where ||= []).push(record.target.nodeName + "." + (record.target.className || record.target.getAttribute("data-label")) + ":" + [...record.removedNodes].map((n) => n.nodeName).join("/")); } }).observe(document.getElementById("targets-table"), { childList: true, subtree: true });
			});
			// Interaction state that must survive: collapse one group, filter, focus a row link.
			await page.click("#targets-table .accordion-button >> nth=0");
			await page.waitForSelector("#targets-table .accordion-button[aria-expanded=false]");
			await page.fill("#targets-search", "luettring");
			await page.waitForSelector("#targets-table tbody tr[hidden]", { state: "attached" });
			const collapsedBefore = await page.evaluate(() => [...document.querySelectorAll("#targets-table .accordion-button")].map((b) => b.getAttribute("aria-expanded")));
			await page.focus("#targets-table tbody tr:not([hidden]) td[data-label=Name] a");
			const focusedHref = await page.evaluate(() => document.activeElement.getAttribute("href"));
			await page.evaluate(() => { window.__shifts = 0; window.__removed = 0; });

			const httpDown = { label: "HTTP", state: "Down" };
			const downBefore = await countStateBadges(page, "#targets-table .ju-status-item", httpDown);
			setMode("changed");
			await page.evaluate(() => window.htmx.trigger("#targets-table", "refresh"));
			await waitForStateBadgeCount(page, "#targets-table .ju-status-item", { ...httpDown, above: downBefore }).catch((error) => { throw getRouteError() ?? error; });
			await waitForVisualStability(page);

			const after = await page.evaluate(() => {
				const rows = [...document.querySelectorAll("#targets-table tbody tr[data-search]")];
				return {
					kept: rows.filter((row) => row.__tag !== undefined).length,
					statusKept: rows.filter((row) => row.querySelector("td[data-label=Status] > span")?.__tag !== undefined).length,
					total: rows.length, expected: window.__rows,
					removed: window.__removed,
					expanded: [...document.querySelectorAll("#targets-table .accordion-button")].map((b) => b.getAttribute("aria-expanded")),
					filter: document.getElementById("targets-search").value,
					hidden: rows.filter((row) => row.hidden).length,
					focus: document.activeElement.getAttribute("href"),
					shifts: window.__shifts,
					unknownIcons: document.querySelectorAll("#targets-table .status-unknown").length,
				};
			});
			assert.equal(after.total, after.expected, "rows were added or lost");
			assert.equal(after.kept, after.total, `rows replaced by the poll: ${after.total - after.kept} of ${after.total}`);
			assert.equal(after.statusKept, after.total, "self-updating status icons were replaced");
			// One Checks cell is replaced: its 5 child nodes (summary and badge rows with their separating text).
			assert.ok(after.removed <= 5, `poll removed ${after.removed} nodes: ${await page.evaluate(() => (window.__where || []).join(" ; "))}`);
			assert.deepEqual(after.expanded, collapsedBefore, "group open/closed state changed");
			assert.equal(after.filter, "luettring", "search text lost");
			assert.ok(after.hidden > 0, "search filter no longer applied");
			assert.equal(after.focus, focusedHref, "focus left the row link");
			assert.equal(after.unknownIcons, 0, "status icons fell back to the unknown placeholder");
			if (test.info().project.name === "chromium") assert.ok(after.shifts <= CLS_BUDGET, `layout shift ${after.shifts}`);
			assert.deepEqual(problems, []);
		} finally {
			await context.close();
		}
	});
}

for (const width of [320, 390]) {
	test(`many checks stay compact and expand across a refresh at ${width}px @targets-live-update`, async ({ browser, app, adminState }) => {
		const { context, page, problems } = await open(browser, app, adminState, { width, height: 844 });
		try {
			// Narrow viewports collapse the groups on load; open them so the row is rendered.
			await openAllGroups(page);
			const row = page.locator('#targets-table tr[data-target-id]', { hasText: 'a-very-long-hostname-without-break-opportunity' });
			await row.waitFor();
			const badges = row.locator('.check-badges .ju-status-item');
			const total = await badges.count();
			assert.ok(total > 14, 'the fixture must contain many checks');
			const summary = await row.locator('.targets-check-summary').innerText();
			assert.match(summary, new RegExp(`\\b${total} Checks\\b`));
			const toggle = row.locator('.targets-check-toggle');
			assert.equal(await toggle.innerText(), `+${total - 3} more`);
			assert.equal(await toggle.evaluate((button) => button.parentElement.classList.contains('check-badges')), true);
			assert.ok((await toggle.boundingBox()).height <= 28, 'the continuation control should fit the badge row');
			const visibleBadges = () => badges.evaluateAll((nodes) => nodes.filter((node) => getComputedStyle(node).display !== 'none').length);
			assert.equal(await visibleBadges(), 3);
			const collapsedHeight = (await row.boundingBox()).height;
			assert.ok(collapsedHeight < 300, `collapsed card is ${collapsedHeight}px high`);
			await toggle.click();
			assert.equal(await toggle.getAttribute('aria-expanded'), 'true');
			assert.equal(await visibleBadges(), total);
			await toggle.focus();
			// An unchanged answer fires no patch event, so the end of the request is the signal.
			await page.evaluate(() => new Promise((resolve, reject) => {
				const table = document.getElementById('targets-table');
				const timer = setTimeout(() => reject(new Error("refresh request did not finish")), 15000);
				table.addEventListener('htmx:afterRequest', () => { clearTimeout(timer); resolve(); }, { once: true });
				window.htmx.trigger(table, 'refresh');
			}));
			assert.equal(await toggle.getAttribute('aria-expanded'), 'true');
			assert.equal(await visibleBadges(), total);
			assert.equal(await toggle.evaluate((node) => document.activeElement === node), true, 'refresh moved focus');
			await toggle.click();
			assert.equal(await toggle.getAttribute('aria-expanded'), 'false');
			assert.equal(await visibleBadges(), 3);
			assert.deepEqual(problems, []);
		} finally {
			await context.close();
		}
	});
}

// ---- Transactions around a refresh (DESIGN.md 8.3 and "Targets list: live-update transactions") ----
// The poll answers below are the real fragment, rewritten per test, so each case is deterministic.
const ROW_PATTERN = '<tr data-search="[^"]*" data-target-id="(\\d+)">[\\s\\S]*?</tr>';
const rowsOf = (body) => [...body.matchAll(new RegExp(ROW_PATTERN, "g"))].map((match) => ({ id: match[1], html: match[0] }));
const dropRow = (body, id) => body.replace(new RegExp(ROW_PATTERN.replace("(\\d+)", id)), "");
function addRowBefore(body, id, newId) {
	const source = rowsOf(body).find((row) => row.id === id).html;
	const copy = source
		.replaceAll(`data-target-id="${id}"`, `data-target-id="${newId}"`)
		.replaceAll(`/target-status/${id}`, `/target-status/${newId}`)
		.replaceAll(`justup-target-${id} `, `justup-target-${newId} `)
		.replaceAll(`target-checks-${id}`, `target-checks-${newId}`)
		.replaceAll(`/uptime/${id}`, `/uptime/${newId}`);
	// The clone has no status endpoint: a static cell keeps its height from changing after the swap.
	const settled = copy.replace(/<span hx-get="[^"]*target-status[^"]*"[^>]*>/, "<span>");
	return body.replace(source, settled + source);
}
// A two-page world on top of the single-page seed: page 2 lacks the first row of page 1.
function pagedWorld(body, page) {
	const first = rowsOf(body)[0]?.id;
	if (first === undefined) throw new Error(`the fragment carries no rows: ${body.slice(0, 160)}`);
	let out = body.replace('data-total-pages="1"', 'data-total-pages="2"').replace(/(id="targets-page-state"[^>]*value=")\d+/, `$1${page}`);
	if (page === 2) out = dropRow(out, first);
	const button = (label, target, icon, off) => `<li class="page-item${off ? " disabled" : ""}"><button type="button" class="page-link" aria-label="${label}" hx-get="/ui/fragments/targets-table?page=${target}" hx-target="#targets-table" hx-swap="innerHTML"${off ? " disabled" : ""}><span class="material-icons" aria-hidden="true">${icon}</span></button></li>`;
	return out + `<div class="d-flex justify-content-between align-items-center mt-3 px-2" hx-disinherit="hx-include"><div class="text-body-secondary small">Page ${page} of 2</div><ul class="pagination pagination-sm m-0">${button("Previous page", page - 1, "chevron_left", page <= 1)}${button("Next page", page + 1, "chevron_right", page >= 2)}</ul></div>`;
}

// State signal for a condition that the test cannot await on the page (a request that was seen by the route).
const until = (condition) => expect.poll(condition, { timeout: 10_000 }).toBe(true);

const SHORT_VIEWPORTS = [{ width: 1280, height: 360 }, { width: 390, height: 520 }];

// Tags rows and the accordion, scrolls <main> and watches every write below the container's own element.
// A narrow viewport starts with collapsed groups; the list only scrolls with every group open.
async function openAllGroups(page) {
	const collapsed = page.locator("#targets-table .accordion-button[aria-expanded=false]");
	while (await collapsed.count()) {
		await collapsed.first().click();
		await page.waitForFunction(() => !document.querySelector("#targets-table .collapsing"));
	}
}

async function arm(page, scrollTo) {
	await openAllGroups(page);
	return page.evaluate((target) => {
		const main = document.getElementById("main-content");
		const room = main.scrollHeight - main.clientHeight;
		if (room < 120) throw new Error(`the fixture list must scroll in <main> (room ${room}px)`);
		main.scrollTop = target === "end" ? room : Math.min(target, room);
		document.querySelectorAll("#targets-table tr[data-target-id]").forEach((row) => { row.__node = true; });
		const accordion = document.getElementById("targets-accordion");
		accordion.__node = true;
		window.__records = [];
		window.__patched = 0;
		document.getElementById("targets-table").addEventListener("targets:patched", () => { window.__patched += 1; });
		new MutationObserver((list) => window.__records.push(...list.map((record) => `${record.type}:${record.target.nodeName}`))).observe(accordion, { childList: true, attributes: true, characterData: true, subtree: true });
		return main.scrollTop;
	}, scrollTo);
}

// The first row that reaches into <main>, and where it sits relative to the top of <main>.
const anchorOf = (page) => page.evaluate(() => {
	const top = document.getElementById("main-content").getBoundingClientRect().top;
	for (const row of document.querySelectorAll("#targets-table tr[data-target-id]")) {
		const box = row.getBoundingClientRect();
		if (box.height > 0 && box.bottom > top + 1) return { id: row.dataset.targetId, offset: Math.round(box.top - top) };
	}
	return null;
});
// Focus without scrolling, so a measurement taken before stays valid.
const focusQuietly = (page, selector) => page.locator(selector).evaluate((element) => element.focus({ preventScroll: true }));

// Offset of one row relative to the top of <main>.
const offsetOf = (page, id) => page.evaluate((rowId) => {
	const row = document.querySelector(`#targets-table tr[data-target-id="${rowId}"]`);
	return Math.round(row.getBoundingClientRect().top - document.getElementById("main-content").getBoundingClientRect().top);
}, id);
const scrollOf = (page) => page.evaluate(() => { const main = document.getElementById("main-content"); return { top: main.scrollTop, max: main.scrollHeight - main.clientHeight }; });

// Resolves once the response for the next fragment request was handled by htmx and the request is done.
async function refreshAndSettle(page) {
	const answered = page.waitForResponse((response) => response.url().includes("/ui/fragments/targets-table"));
	await page.evaluate(() => window.htmx.trigger("#targets-table", "refresh"));
	await answered;
	await page.waitForFunction(() => !document.querySelector("#targets-table.htmx-request"));
}

for (const viewport of SHORT_VIEWPORTS) {
	const label = `${viewport.width}x${viewport.height}`;

	test(`an identical answer writes nothing and keeps scroll and focus at ${label} @targets-live-update`, async ({ browser, app, adminState }) => {
		const { context, page, problems } = await open(browser, app, adminState, viewport);
		try {
			const scrollTop = await arm(page, 140);
			await focusQuietly(page, "#targets-table tbody tr:not([hidden]) td[data-label=Name] a >> nth=1");
			const focused = await page.evaluate(() => { document.activeElement.__focus = true; return document.activeElement.getAttribute("href"); });
			await refreshAndSettle(page);
			const after = await page.evaluate(() => ({
				records: window.__records, patched: window.__patched,
				rowsKept: [...document.querySelectorAll("#targets-table tr[data-target-id]")].every((row) => row.__node),
				accordionKept: document.getElementById("targets-accordion").__node === true,
				focusKept: document.activeElement.__focus === true,
			}));
			assert.deepEqual(after.records, [], "an identical answer wrote to the list");
			assert.equal(after.patched, 0, "an identical answer announced a patch");
			assert.ok(after.rowsKept && after.accordionKept, "rows or accordion were replaced");
			assert.ok(after.focusKept, `focus left ${focused}`);
			assert.equal((await scrollOf(page)).top, scrollTop, "scroll position moved");
			assert.deepEqual(problems, []);
		} finally {
			await context.close();
		}
	});

	test(`a changed cell keeps rows, scroll and focus at ${label} @targets-live-update`, async ({ browser, app, adminState }) => {
		let mark = false;
		const { context, page, problems } = await open(browser, app, adminState, viewport, {
			transform: (body) => (mark ? body.replace(/(<td data-label="Name"[^>]*>\s*<a [^>]*>\s*)([^<\s][^<]*?)(\s*<\/a>)/, "$1$2 renamed$3") : body),
		});
		try {
			const scrollTop = await arm(page, 140);
			await focusQuietly(page, "#targets-table tbody tr:not([hidden]) td[data-label=Name] a >> nth=0");
			await page.evaluate(() => { document.activeElement.__focus = true; });
			mark = true;
			await refreshAndSettle(page);
			const after = await page.evaluate(() => ({
				patched: window.__patched,
				rowsKept: [...document.querySelectorAll("#targets-table tr[data-target-id]")].every((row) => row.__node),
				renamed: document.querySelectorAll("#targets-table td[data-label=Name] a").length && [...document.querySelectorAll("#targets-table td[data-label=Name] a")].filter((a) => /renamed/.test(a.textContent)).length,
				focusText: document.activeElement.textContent.trim(),
			}));
			assert.equal(after.renamed, 1, "exactly one name cell changes");
			assert.ok(after.rowsKept, "rows were replaced for a cell change");
			assert.equal(after.patched, 1);
			assert.match(after.focusText, /renamed/, "focus did not follow the rewritten cell");
			assert.equal((await scrollOf(page)).top, scrollTop, "scroll position moved");
			assert.deepEqual(problems, []);
		} finally {
			await context.close();
		}
	});

	test(`a removed target above the anchor keeps the anchor offset at ${label} @targets-live-update`, async ({ browser, app, adminState }) => {
		let drop = null;
		const { context, page, problems } = await open(browser, app, adminState, viewport, { transform: (body) => (drop ? dropRow(body, drop) : body) });
		try {
			await arm(page, "end");
			const before = await anchorOf(page);
			const ids = await page.evaluate(() => [...document.querySelectorAll("#targets-table tr[data-target-id]")].map((row) => row.dataset.targetId));
			drop = ids[ids.indexOf(before.id) - 1] ?? ids[0];
			assert.notEqual(drop, before.id, "the fixture needs a row above the anchor");
			await refreshAndSettle(page);
			// The swapped rows start with status and uptime placeholders that htmx fills in with their own requests.
			// Measure the settled list, as the "before" reading was, not a list that is still loading its cells.
			await page.waitForFunction(() => !document.querySelector("#targets-table .status-unknown, #targets-table [aria-label='Loading uptime']"));
			await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
			const after = await anchorOf(page);
			assert.equal(after.id, before.id, "the anchor row changed");
			assert.ok(Math.abs(after.offset - before.offset) <= 1, `anchor moved from ${before.offset}px to ${after.offset}px`);
			assert.ok((await scrollOf(page)).top > 0, "the list jumped to the top");
			assert.equal(await page.evaluate(() => document.querySelectorAll("#targets-table tr[data-target-id]").length), ids.length - 1);
			assert.deepEqual(problems, []);
		} finally {
			await context.close();
		}
	});

	test(`an added target above the anchor keeps the anchor offset and the focused control at ${label} @targets-live-update`, async ({ browser, app, adminState }) => {
		let add = false;
		const { context, page, problems } = await open(browser, app, adminState, viewport, {
			transform: (body) => (add ? addRowBefore(body, rowsOf(body)[0].id, "9001") : body),
		});
		try {
			await arm(page, 140);
			const before = await anchorOf(page);
			await focusQuietly(page, `#targets-table tr[data-target-id="${before.id}"] td[data-label=Name] a`);
			add = true;
			await refreshAndSettle(page);
			assert.equal(await page.locator("#targets-table tr[data-target-id='9001']").count(), 1, "the added row is missing");
			const after = await offsetOf(page, before.id);
			assert.ok(Math.abs(after - before.offset) <= 1, `anchor moved from ${before.offset}px to ${after}px`);
			assert.equal(await page.evaluate(() => document.activeElement.closest("tr")?.dataset.targetId), before.id, "focus left the control of the same target");
			assert.deepEqual(problems, []);
		} finally {
			await context.close();
		}
	});

	test(`a removed focused target hands focus to the next row, then the previous one at ${label} @targets-live-update`, async ({ browser, app, adminState }) => {
		let drop = null;
		const { context, page, problems } = await open(browser, app, adminState, viewport, { transform: (body) => (drop ? dropRow(body, drop) : body) });
		try {
			await openAllGroups(page);
			const ids = await page.evaluate(() => [...document.querySelectorAll("#targets-table tr[data-target-id]")].filter((row) => row.getClientRects().length).map((row) => row.dataset.targetId));
			assert.ok(ids.length >= 3, "the fixture needs three rendered rows");
			const focusRow = async (id) => { await page.focus(`#targets-table tr[data-target-id="${id}"] td[data-label=Name] a`); drop = id; await refreshAndSettle(page); };
			const focusedId = () => page.evaluate(() => document.activeElement.closest("tr")?.dataset.targetId ?? document.activeElement.id);
			await focusRow(ids[1]);
			assert.equal(await focusedId(), ids[2], "focus did not move to the next row");
			// The last row has no next row: the previous one takes over.
			await focusRow(ids[ids.length - 1]);
			assert.equal(await focusedId(), ids[ids.length - 2], "focus did not move to the previous row");
			assert.deepEqual(problems, []);
		} finally {
			await context.close();
		}
	});

	test(`a shorter list near the end clamps and does not jump to the top at ${label} @targets-live-update`, async ({ browser, app, adminState }) => {
		let drop = [];
		const { context, page, problems } = await open(browser, app, adminState, viewport, { transform: (body) => drop.reduce(dropRow, body) });
		try {
			await arm(page, "end");
			const ids = await page.evaluate(() => [...document.querySelectorAll("#targets-table tr[data-target-id]")].map((row) => row.dataset.targetId));
			drop = ids.slice(-2);
			await refreshAndSettle(page);
			const state = await scrollOf(page);
			assert.ok(state.top > 0, "the list jumped to the top");
			assert.ok(state.top >= state.max - 1, `scroll ${state.top}px is not at the new end ${state.max}px`);
			assert.deepEqual(problems, []);
		} finally {
			await context.close();
		}
	});

	test(`a changing group, open groups and the filter survive one swap at ${label} @targets-live-update`, async ({ browser, app, adminState }) => {
		let rename = false;
		const { context, page, problems } = await open(browser, app, adminState, viewport, {
			transform: (body) => {
				if (!rename) return body;
				const keys = [...body.matchAll(/data-group-key="(targets_group_[^"]+)"/g)].map((match) => match[1]);
				return body.replaceAll(keys[keys.length - 1], `${keys[keys.length - 1]}_renamed`);
			},
		});
		try {
			assert.ok(await page.locator("#targets-table .accordion-item").count() >= 2, "the fixture needs two groups");
			// A narrow viewport starts with collapsed groups, so the click flips whatever state the group has.
			const initial = await page.locator("#targets-table .accordion-button").first().getAttribute("aria-expanded");
			await page.click("#targets-table .accordion-button >> nth=0");
			await page.waitForFunction(() => !document.querySelector("#targets-table .collapsing"));
			const manual = await page.locator("#targets-table .accordion-button").first().getAttribute("aria-expanded");
			assert.notEqual(manual, initial, "the click did not toggle the group");
			await page.fill("#targets-search", "luettring");
			await page.waitForSelector("#targets-table tbody tr[hidden]", { state: "attached" });
			const hiddenBefore = await page.locator("#targets-table tbody tr[hidden]").count();
			await page.evaluate(() => { document.querySelector("#targets-table tr[data-target-id]").__node = true; });
			rename = true;
			await refreshAndSettle(page);
			assert.equal(await page.evaluate(() => document.querySelector("#targets-table tr[data-target-id]").__node === true), false, "a changed group must take the swap path");
			assert.equal(await page.inputValue("#targets-search"), "luettring", "search text lost");
			assert.equal(await page.locator("#targets-table tbody tr[hidden]").count(), hiddenBefore, "the filter no longer applies");
			// Leaving the search restores the group the user closed before.
			await page.fill("#targets-search", "");
			assert.equal(await page.locator("#targets-table tbody tr[hidden]").count(), 0);
			assert.equal(await page.locator("#targets-table .accordion-button").first().getAttribute("aria-expanded"), manual, "the manual group state was lost");
			assert.deepEqual(problems, []);
		} finally {
			await context.close();
		}
	});

	test(`a superseded poll never replaces a newer pager page, and a late pager answer still wins at ${label} @targets-live-update`, async ({ browser, app, adminState }) => {
		const held = [];
		const gate = { poll: false, pager: false };
		const requests = [];
		const { context, page, problems } = await open(browser, app, adminState, viewport, {
			transform: async (body, url) => {
				const pageNumber = Number(url.searchParams.get("page") ?? 1);
				const isPoll = url.searchParams.has("all");
				requests.push(`${isPoll ? "poll" : "pager"}:${pageNumber}`);
				const out = pagedWorld(body, pageNumber);
				if ((isPoll && gate.poll) || (!isPoll && gate.pager)) await new Promise((resolve) => held.push(resolve));
				return out;
			},
		});
		try {
			const state = () => page.evaluate(() => ({
				text: document.querySelector("#targets-table .text-body-secondary.small")?.textContent.trim(),
				rows: document.querySelectorAll("#targets-table tr[data-target-id]").length,
				carrier: document.getElementById("targets-page-state").value,
			}));
			const first = await state();
			assert.equal(first.text, "Page 1 of 2");
			// A poll for page 1 is in flight while the user moves to page 2; the poll answers last.
			gate.poll = true;
			await page.evaluate(() => window.htmx.trigger("#targets-table", "refresh"));
			await until(() => requests.filter((r) => r === "poll:1").length >= 2);
			await page.focus('#targets-table .page-link[aria-label="Next page"]');
			await page.keyboard.press("Enter");
			await page.waitForFunction(() => document.getElementById("targets-page-state").value === "2");
			gate.poll = false;
			const second = await state();
			assert.equal(second.text, "Page 2 of 2");
			assert.equal(second.rows, first.rows - 1);
			held.splice(0).forEach((release) => release());
			await page.waitForFunction(() => !document.querySelector("#targets-table.htmx-request"));
			await until(() => requests.filter((r) => r === "poll:2").length >= 1);
			await page.waitForFunction(() => !document.querySelector("#targets-table.htmx-request"));
			assert.deepEqual(await state(), second, "the superseded poll replaced the newer page");

			// Now the pager answer is held while a poll for the old page answers first.
			gate.pager = true;
			await page.focus('#targets-table .page-link[aria-label="Previous page"]');
			await page.keyboard.press("Enter");
			await until(() => held.length === 1);
			await page.evaluate(() => window.htmx.trigger("#targets-table", "refresh"));
			await until(() => requests.filter((r) => r === "poll:2").length >= 2);
			gate.pager = false;
			held.splice(0).forEach((release) => release());
			await page.waitForFunction(() => document.getElementById("targets-page-state").value === "1");
			await page.waitForFunction(() => !document.querySelector("#targets-table.htmx-request"));
			assert.equal((await state()).text, "Page 1 of 2", "the late pager answer was dropped");
			assert.deepEqual(problems, []);
		} finally {
			held.splice(0).forEach((release) => release());
			await context.close();
		}
	});
}

test(`refresh events inside the throttle window collapse into one trailing refresh @targets-live-update`, async ({ browser, app, adminState }) => {
	let requests = 0;
	const { context, page, problems } = await open(browser, app, adminState, { width: 1280, height: 720 }, { transform: (body) => { requests += 1; return body; } });
	try {
		const baseline = requests;
		// Three events in the same window: the first starts a refresh, the other two leave one trailing refresh.
		await page.evaluate(() => { for (let i = 0; i < 3; i += 1) document.body.dispatchEvent(new CustomEvent("justup-target-list")); });
		await until(() => requests - baseline >= 2);
		await page.waitForFunction(() => !document.querySelector("#targets-table.htmx-request"));
		assert.equal(requests - baseline, 2, "the three events must give a leading and one trailing refresh");
		assert.deepEqual(problems, []);
	} finally {
		await context.close();
	}
});

for (const viewport of SHORT_VIEWPORTS) {
	test(`a pager click starts at the top of the list and keeps focus on a pager button at ${viewport.width}x${viewport.height} @targets-live-update`, async ({ browser, app, adminState }) => {
		const { context, page, problems } = await open(browser, app, adminState, viewport, { transform: (body, url) => pagedWorld(body, Number(url.searchParams.get("page") ?? 1)) });
		try {
			await arm(page, "end");
			await page.focus('#targets-table .page-link[aria-label="Next page"]');
			await page.evaluate(() => { document.getElementById("main-content").scrollTop = 1e6; });
			await page.keyboard.press("Enter");
			await page.waitForFunction(() => document.getElementById("targets-page-state").value === "2");
			await page.waitForFunction(() => !document.querySelector("#targets-table.htmx-request"));
			const pageNumber = await page.evaluate(() => document.querySelector("#targets-table .text-body-secondary.small").textContent.trim());
			assert.equal(pageNumber, "Page 2 of 2");
			const listTop = await page.evaluate(() => Math.round(document.getElementById("targets-table").getBoundingClientRect().top - document.getElementById("main-content").getBoundingClientRect().top));
			assert.ok(listTop >= -1, `the new page does not start at the top of the list (${listTop}px)`);
			// Next is disabled on the last page, so focus moves to the remaining pager button instead of body.
			assert.equal(await page.evaluate(() => document.activeElement.getAttribute("aria-label")), "Previous page");
			assert.deepEqual(problems, []);
		} finally {
			await context.close();
		}
	});
}
