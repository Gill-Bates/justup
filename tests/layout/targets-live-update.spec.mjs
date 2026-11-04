//
// tests/layout/targets-live-update.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Targets page live refresh (DESIGN.md, "Load stability"): a poll that changes one status rewrites
// only the changed cell. Unchanged rows, the self-updating status icons, focus, collapsed groups and
// the search filter all stay as they are, and nothing shifts. The poll answer is the real fragment
// with one HTTP badge switched to "down".
import assert from "node:assert/strict";
import { test } from "./fixtures.mjs";
import { waitForVisualStability } from "./harness.mjs";

const CLS_BUDGET = 0.01;

async function open(browser, app, adminState, viewport) {
	const context = await browser.newContext({ viewport, storageState: adminState });
	const page = await context.newPage();
	const problems = [];
	page.on("pageerror", (error) => problems.push(String(error)));
	await page.addInitScript(() => {
		window.__shifts = 0;
		try { new PerformanceObserver((list) => { for (const entry of list.getEntries()) if (!entry.hadRecentInput) window.__shifts += entry.value; }).observe({ type: "layout-shift", buffered: true }); } catch { /* no layout-shift entries */ }
	});
	let mode = "pass";
	await page.route("**/ui/fragments/targets-table**", async (route) => {
		const response = await route.fetch();
		let body = await response.text();
		if (mode === "changed") {
			// Switch the first HTTP badge that is not yet "down" to down: one cell changes.
			body = body.replace(/(<span class="badge )ju-status-badge--(?:success|neutral)(">HTTP<\/span>)/, "$1ju-status-badge--danger$2");
		}
		await route.fulfill({ response, body });
	});
	await page.goto(app.base + "/ui/targets", { waitUntil: "load" });
	await page.waitForFunction(() => document.querySelectorAll("#targets-table tbody tr[data-search]").length >= 4, null, { timeout: 20000 });
	await page.waitForFunction(() => !document.querySelector("#targets-table .status-unknown"), null, { timeout: 20000 }).catch(() => {});
	await waitForVisualStability(page);
	return { context, page, problems, setMode: (value) => { mode = value; } };
}

for (const viewport of [{ width: 1280, height: 720 }, { width: 390, height: 844 }]) {
	test(`a changed poll rewrites only the changed cell at ${viewport.width}x${viewport.height} @targets-live-update`, async ({ browser, app, adminState }) => {
		const { context, page, problems, setMode } = await open(browser, app, adminState, viewport);
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
			await page.waitForTimeout(500);
			await page.fill("#targets-search", "luettring");
			await page.waitForTimeout(300);
			const collapsedBefore = await page.evaluate(() => [...document.querySelectorAll("#targets-table .accordion-button")].map((b) => b.getAttribute("aria-expanded")));
			await page.focus("#targets-table tbody tr:not([hidden]) td[data-label=Name] a");
			const focusedHref = await page.evaluate(() => document.activeElement.getAttribute("href"));
			await page.evaluate(() => { window.__shifts = 0; window.__removed = 0; });

			setMode("changed");
			await page.evaluate(() => window.htmx.trigger("#targets-table", "refresh"));
			await page.waitForFunction(() => [...document.querySelectorAll("#targets-table .badge.ju-status-badge--danger")].some((b) => b.textContent === "HTTP"), null, { timeout: 10000 });
			await page.waitForTimeout(600);

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
			assert.ok(after.removed <= 4, `poll removed ${after.removed} nodes: ${await page.evaluate(() => (window.__where || []).join(" ; "))}`);
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
