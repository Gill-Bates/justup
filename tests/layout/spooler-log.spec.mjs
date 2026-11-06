//
// tests/layout/spooler-log.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Delivery Queue Log (DESIGN.md, table contract): a narrow container shows one card per entry with
// label/value rows, a wide one the table. The channel is an icon with an accessible name in the table
// and icon plus visible name in the card.
import assert from "node:assert/strict";
import { expect } from "@playwright/test";
import { test } from "./fixtures.mjs";
import { waitForVisualStability } from "./harness.mjs";

const NOW = new Date().toISOString();
const ENTRIES = [
	{ id: 1, status: "sent", channel_type: "email", notification_type: "alert", retry_count: 0, updated_at: NOW, recipient_name: "Martin", recipient_address: "m@example.org", target_name: "Example Web", target_id: 1, last_error: null },
	{ id: 2, status: "sent", channel_type: "signal", notification_type: "alert", retry_count: 0, updated_at: NOW, recipient_name: "Tobias", recipient_address: "+4915112345678", target_name: "Example Web", target_id: 1, last_error: null },
	{ id: 3, status: "failed", channel_type: "email", notification_type: "test", retry_count: 3, updated_at: NOW, recipient_name: "Martin", recipient_address: "m@example.org", target_name: null, target_id: null, last_error: "Connection refused by a server with a rather long error text" },
];
const VIEWPORTS = [
	{ name: "1280x720", width: 1280, height: 720, card: false },
	{ name: "390x844", width: 390, height: 844, card: true },
	{ name: "320x640", width: 320, height: 640, card: true },
];

for (const viewport of VIEWPORTS) {
	for (const theme of ["light", "dark"]) {
		test(`delivery queue log ${viewport.card ? "cards" : "table"} at ${viewport.name} ${theme} @spooler-log`, async ({ browser, app, adminState }) => {
			const context = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height }, storageState: adminState });
			const page = await context.newPage();
			const errors = [];
			page.on("pageerror", (error) => errors.push(String(error)));
			await page.route("**/settings/notification-queue/log**", (route) => route.fulfill({ json: { entries: ENTRIES, total: 3 } }));
			try {
				await page.goto(app.base + "/ui/settings", { waitUntil: "load" });
				await page.evaluate((mode) => document.documentElement.setAttribute("data-bs-theme", mode), theme);
				await page.click("#spooler-tab");
				await page.waitForSelector("#spooler-log-body td[data-label=Channel]", { timeout: 15000 });
				await waitForVisualStability(page);
				const m = await page.evaluate(() => {
					const table = document.getElementById("spooler-log-table");
					const row = table.querySelector("tbody tr");
					const cell = row.querySelector("td[data-label=Channel]");
					const name = cell.querySelector(".visually-hidden");
					const nameBox = name.getBoundingClientRect();
					const viewportEl = table.closest(".ju-table-viewport");
					const de = document.documentElement;
					return {
						rowDisplay: getComputedStyle(row).display,
						label: getComputedStyle(cell, "::before").content,
						nameWidth: nameBox.width,
						nameHeight: nameBox.height,
						nameText: name.textContent.trim(),
						cells: [...row.children].map((c) => c.dataset.label),
						pageOverflow: de.scrollWidth > de.clientWidth + 1,
						regionOverflow: viewportEl.scrollWidth > viewportEl.clientWidth + 1,
						icon: cell.querySelector(".material-icons")?.getAttribute("aria-hidden"),
						channelColumnWidth: cell.getBoundingClientRect().width,
						labelSizes: [...row.children].map((c) => getComputedStyle(c, "::before").fontSize),
					};
				});
				assert.equal(m.pageOverflow, false, "the page scrolls horizontally");
				assert.ok(m.cells.every(Boolean), `every cell carries its data-label: ${m.cells}`);
				assert.equal(m.icon, "true", "the channel icon is decorative");
				if (viewport.card) {
					assert.equal(m.rowDisplay, "block", "narrow container: one card per entry");
					assert.equal(m.label, '"Channel"', "the card row is labelled by data-label");
					assert.ok(m.nameWidth > 20 && m.nameHeight > 8, `the channel name is visible in the card (${m.nameWidth}x${m.nameHeight})`);
					assert.equal(m.regionOverflow, false, "the cards fit without horizontal scrolling");
					assert.equal(new Set(m.labelSizes).size, 1, `every card label has the same size: ${m.labelSizes}`);
				} else {
					assert.equal(m.rowDisplay, "table-row", "wide container: the table");
					assert.ok(m.nameWidth <= 1 && m.nameHeight <= 1, "the channel name is not visible in the table");
					assert.ok(m.channelColumnWidth <= 64, `the channel column stays narrow (${m.channelColumnWidth}px)`);
				}
				assert.deepEqual(errors, []);
			} finally {
				await context.close();
			}
		});
	}
}

// Toolbar and empty state: title row (title, Refresh), filter row (Status, Channel), management row
// (Clear log, Flush queue); an empty log is a centred message, not a label/value card row.
const TOOLBAR_VIEWPORTS = [
	{ name: "320x640", width: 320, height: 640 },
	{ name: "390x844", width: 390, height: 844 },
	{ name: "1280x720", width: 1280, height: 720 },
	// 200 % zoom of a 1280 px window is a 640 CSS px viewport.
	{ name: "200%-zoom", width: 640, height: 360 },
];

async function openSpooler(page, app, theme, payload) {
	await page.route("**/settings/notification-queue/log**", (route) => route.fulfill({ json: payload }));
	await page.goto(app.base + "/ui/settings", { waitUntil: "load" });
	await page.evaluate((mode) => document.documentElement.setAttribute("data-bs-theme", mode), theme);
	await page.click("#spooler-tab");
	await page.waitForSelector("#spooler-log-body tr:not(:has(.spinner-border))", { timeout: 15000 });
	await page.waitForTimeout(300);
}

for (const viewport of TOOLBAR_VIEWPORTS) {
	for (const theme of ["light", "dark"]) {
		test(`delivery queue log toolbar at ${viewport.name} ${theme} @spooler-log`, async ({ browser, app, adminState }) => {
			const context = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height }, storageState: adminState });
			const page = await context.newPage();
			const errors = [];
			page.on("pageerror", (error) => errors.push(String(error)));
			try {
				await openSpooler(page, app, theme, { entries: ENTRIES, total: 3 });
				const toolbar = page.locator(".spooler-toolbar");
				const refresh = toolbar.getByRole("button", { name: "Refresh", exact: true });
				const clear = toolbar.getByRole("button", { name: "Clear log", exact: true });
				const flush = toolbar.getByRole("button", { name: "Flush queue", exact: true });
				const status = toolbar.getByLabel("Status", { exact: true });
				const channel = toolbar.getByLabel("Channel", { exact: true });
				for (const control of [refresh, clear, flush, status, channel]) await control.scrollIntoViewIfNeeded();
				const m = await page.evaluate(() => {
					const box = (selector) => {
						const r = document.querySelector(selector).getBoundingClientRect();
						return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width, height: r.height };
					};
					const de = document.documentElement;
					const t = document.querySelector(".spooler-toolbar");
					return {
						pageOverflow: de.scrollWidth > de.clientWidth + 1,
						toolbarOverflow: t.scrollWidth > t.clientWidth + 1,
						toolbarWidth: t.clientWidth,
						title: box(".spooler-toolbar h2"),
						titleLines: Math.round(document.querySelector(".spooler-toolbar h2").getBoundingClientRect().height / parseFloat(getComputedStyle(document.querySelector(".spooler-toolbar h2")).lineHeight)),
						refresh: box("#spooler-refresh-btn"),
						status: box("#spooler-filter-status"),
						channel: box("#spooler-filter-channel"),
						clear: box("#spooler-clear-log-btn"),
						flush: box("#spooler-flush-btn"),
						empty: false,
					};
				});
				assert.equal(m.pageOverflow, false, "the page scrolls horizontally");
				assert.equal(m.toolbarOverflow, false, "the toolbar overflows its container");
				assert.equal(m.titleLines, 1, "the title stays on one line");
				const controls = ["title", "refresh", "status", "channel", "clear", "flush"];
				for (let i = 0; i < controls.length; i += 1) {
					for (let j = i + 1; j < controls.length; j += 1) {
						const a = m[controls[i]];
						const b = m[controls[j]];
						const overlap = a.left < b.right - 0.5 && b.left < a.right - 0.5 && a.top < b.bottom - 0.5 && b.top < a.bottom - 0.5;
						assert.equal(overlap, false, `${controls[i]} overlaps ${controls[j]}`);
					}
				}
				assert.ok(Math.abs(m.status.width - m.channel.width) <= 1, `filters have equal width (${m.status.width} vs ${m.channel.width})`);
				for (const name of controls.slice(1)) assert.ok(m[name].left >= -0.5 && m[name].right <= viewport.width + 0.5, `${name} is inside the viewport`);
				assert.ok(m.status.width >= 100, `the Status filter is usable (${m.status.width}px)`);
				if (m.toolbarWidth < 640) {
					assert.ok(m.title.bottom <= m.status.top + 1 && m.status.bottom <= m.clear.top + 1, "narrow: title row, filter row, action row");
					assert.ok(Math.abs(m.refresh.right - m.channel.right) <= 1, "narrow: Refresh ends at the right edge of the filters");
					assert.ok(Math.abs(m.flush.right - m.channel.right) <= 1, "narrow: the actions are right-aligned");
					assert.ok(Math.abs(m.refresh.top - m.title.top) < m.title.height + 12, "narrow: Refresh sits in the title row");
				} else {
					assert.ok(Math.abs(m.status.top - m.clear.top) <= 12 || m.status.bottom > m.clear.top, "wide: filters and actions share a row");
					assert.ok(m.clear.left > m.channel.right, "wide: the actions are separate from the filters");
				}
				assert.equal(m.refresh.height >= 24 && m.clear.height >= 24 && m.flush.height >= 24, true, "targets are at least 24 px high");
				await status.selectOption("failed");
				await channel.selectOption("email");
				assert.equal(await status.inputValue(), "failed");
				assert.deepEqual(errors, []);
			} finally {
				await context.close();
			}
		});

		test(`delivery queue log empty state at ${viewport.name} ${theme} @spooler-log`, async ({ browser, app, adminState }) => {
			const context = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height }, storageState: adminState });
			const page = await context.newPage();
			try {
				await openSpooler(page, app, theme, { entries: [], total: 0 });
				const m = await page.evaluate(() => {
					const cell = document.querySelector("#spooler-log-body td");
					const range = document.createRange();
					range.selectNodeContents(cell);
					const text = range.getBoundingClientRect();
					const box = cell.getBoundingClientRect();
					const de = document.documentElement;
					return {
						text: cell.textContent.trim(),
						align: getComputedStyle(cell).textAlign,
						textCenter: (text.left + text.right) / 2,
						cellCenter: (box.left + box.right) / 2,
						cellHeight: box.height,
						pageOverflow: de.scrollWidth > de.clientWidth + 1,
					};
				});
				assert.equal(m.text, "No entries");
				assert.ok(Math.abs(m.textCenter - m.cellCenter) <= 2, `the message is centred (${m.textCenter} vs ${m.cellCenter})`);
				assert.ok(m.cellHeight >= 56, `the empty state has room (${m.cellHeight}px)`);
				assert.equal(m.pageOverflow, false, "the page scrolls horizontally");
			} finally {
				await context.close();
			}
		});
	}
}

test("delivery queue log toolbar actions keep filters, refresh and confirmations @spooler-log", async ({ browser, app, adminState }) => {
	const context = await browser.newContext({ viewport: { width: 390, height: 844 }, storageState: adminState });
	const page = await context.newPage();
	const requests = [];
	page.on("request", (request) => requests.push(request.method() + " " + new URL(request.url()).pathname + new URL(request.url()).search));
	try {
		await openSpooler(page, app, "light", { entries: ENTRIES, total: 3 });
		const toolbar = page.locator(".spooler-toolbar");
		const logCalls = () => requests.filter((r) => r.includes("/notification-queue/log"));
		const nextLogCall = (check = () => true) => page.waitForRequest((r) => r.url().includes("/notification-queue/log") && check(r.url()));
		const filtered = nextLogCall((url) => url.includes("status=failed"));
		await toolbar.getByLabel("Status", { exact: true }).selectOption("failed");
		await filtered;
		const before = logCalls().length;
		const refreshed = nextLogCall();
		await toolbar.getByRole("button", { name: "Refresh", exact: true }).click();
		await refreshed;
		assert.ok(logCalls().length > before, "Refresh reloads the log");

		await toolbar.getByRole("button", { name: "Clear log", exact: true }).click();
		await page.locator("#spoolerClearLogModal.show").waitFor();
		assert.equal(requests.some((r) => r.includes("clear-log")), false, "Clear log asks before it deletes");
		await page.locator("#spoolerClearLogModal [data-bs-dismiss=modal]:has-text('Cancel')").click();
		await page.locator("#spoolerClearLogModal.show").waitFor({ state: "detached" }).catch(() => {});
		await page.waitForTimeout(400);

		await toolbar.getByRole("button", { name: "Flush queue", exact: true }).click();
		const dialog = page.locator(".modal.show");
		await dialog.waitFor();
		assert.equal(requests.some((r) => r.includes("/flush")), false, "Flush queue asks before it deletes");
		await dialog.locator("#ju-confirm-cancel").click();
		await page.waitForTimeout(400);
		assert.equal(requests.some((r) => r.includes("/flush") || r.includes("clear-log")), false, "cancelling deletes nothing");
	} finally {
		await context.close();
	}
});

test("delivery queue log distinguishes an empty log from a filter without matches @spooler-log", async ({ browser, app, adminState }) => {
	const context = await browser.newContext({ viewport: { width: 390, height: 844 }, storageState: adminState });
	const page = await context.newPage();
	try {
		await openSpooler(page, app, "light", { entries: [], total: 0 });
		assert.equal((await page.locator("#spooler-log-body td").textContent()).trim(), "No entries");
		await page.locator("#spooler-filter-status").selectOption("failed");
		await page.waitForFunction(() => document.querySelector("#spooler-log-body td")?.textContent.trim() === "No matching entries", null, { timeout: 5000 });
	} finally {
		await context.close();
	}
});

// State row (loading spinner, "No entries", "No matching entries"): in table mode (container >= 40rem)
// the cell is a real table cell spanning every column (colspan) and centred; only in card mode
// (container < 40rem) it is a block with no label. Measured in both modes, both themes, all three states.
const STATE_VIEWPORTS = [
	{ name: "1280x720", width: 1280, height: 720, card: false },
	{ name: "1024x768", width: 1024, height: 768, card: false },
	{ name: "390x844", width: 390, height: 844, card: true },
	{ name: "320x640", width: 320, height: 640, card: true },
];
const STATES = ["empty", "filtered", "loading"];

// Opens the spooler tab. "loading" holds the log response until `release()` so the spinner row can be measured.
async function openStateRow(page, app, theme, state) {
	let release = () => {};
	const gate = state === "loading" ? new Promise((resolve) => { release = resolve; }) : Promise.resolve();
	await page.route("**/settings/notification-queue/log**", async (route) => {
		await gate;
		await route.fulfill({ json: { entries: [], total: 0 } });
	});
	await page.goto(app.base + "/ui/settings", { waitUntil: "load" });
	await page.evaluate((mode) => document.documentElement.setAttribute("data-bs-theme", mode), theme);
	await page.click("#spooler-tab");
	if (state === "loading") {
		await page.waitForSelector("#spooler-log-body td.spooler-log__state .spinner-border", { state: "visible", timeout: 15000 });
	} else {
		await page.waitForFunction(() => document.querySelector("#spooler-log-body td")?.textContent.trim() === "No entries", null, { timeout: 15000 });
		if (state === "filtered") {
			await page.locator("#spooler-filter-status").selectOption("failed");
			await page.waitForFunction(() => document.querySelector("#spooler-log-body td")?.textContent.trim() === "No matching entries", null, { timeout: 15000 });
		}
	}
	await waitForVisualStability(page);
	return release;
}

const measureStateRow = (page) => page.evaluate(() => {
	const table = document.getElementById("spooler-log-table");
	const row = table.querySelector("tbody tr");
	const cell = row.querySelector("td.spooler-log__state");
	const rect = (el) => el.getBoundingClientRect();
	const content = cell.querySelector(".spinner-border");
	let contentBox;
	if (content) {
		contentBox = rect(content);
	} else {
		const range = document.createRange();
		range.selectNodeContents(cell);
		contentBox = range.getBoundingClientRect();
	}
	const rowStyle = getComputedStyle(row);
	const de = document.documentElement;
	const t = rect(table);
	const c = rect(cell);
	const head = rect(table.querySelector("thead"));
	return {
		display: getComputedStyle(cell).display,
		colspan: cell.colSpan,
		before: getComputedStyle(cell, "::before").content,
		tableWidth: t.width,
		tableCentre: (t.left + t.right) / 2,
		cellWidth: c.width,
		cellCentre: (c.left + c.right) / 2,
		cellLeft: c.left,
		cellRight: c.right,
		contentCentre: (contentBox.left + contentBox.right) / 2,
		rowContentWidth: row.clientWidth - parseFloat(rowStyle.paddingLeft) - parseFloat(rowStyle.paddingRight),
		headWidth: head.width,
		headHeight: head.height,
		pageOverflow: de.scrollWidth > de.clientWidth + 1,
		containerWidth: table.closest(".ju-table-viewport").clientWidth,
		viewportWidth: de.clientWidth,
	};
});

function assertStateRow(m, card, label) {
	assert.equal(m.pageOverflow, false, `${label}: the page scrolls horizontally`);
	assert.ok(["none", "normal"].includes(m.before), `${label}: no label content before the message (${m.before})`);
	assert.ok(Math.abs(m.contentCentre - m.cellCentre) <= 2, `${label}: the message is centred in its cell (${m.contentCentre} vs ${m.cellCentre})`);
	if (card) {
		assert.equal(m.display, "block", `${label}: card mode: the state cell is a block`);
		assert.ok(Math.abs(m.cellWidth - m.rowContentWidth) <= 2, `${label}: card mode: the cell fills its row (${m.cellWidth} vs ${m.rowContentWidth})`);
		assert.ok(m.cellLeft >= -0.5 && m.cellRight <= m.viewportWidth + 0.5, `${label}: card mode: the cell is not clipped by the viewport`);
		assert.ok(m.headWidth <= 1 && m.headHeight <= 1, `${label}: card mode: the table header is clipped away (${m.headWidth}x${m.headHeight})`);
	} else {
		assert.equal(m.display, "table-cell", `${label}: table mode: the state cell is a table cell`);
		assert.equal(m.colspan, 8, `${label}: table mode: the cell spans every column`);
		assert.ok(m.cellWidth >= m.tableWidth - 2, `${label}: table mode: the cell spans the full table (${m.cellWidth} vs ${m.tableWidth})`);
		assert.ok(Math.abs(m.cellCentre - m.tableCentre) <= 2, `${label}: table mode: the cell is centred in the table (${m.cellCentre} vs ${m.tableCentre})`);
	}
}

for (const viewport of STATE_VIEWPORTS) {
	for (const theme of ["light", "dark"]) {
		for (const state of STATES) {
			test(`delivery queue log ${state} row in ${viewport.card ? "card" : "table"} mode at ${viewport.name} ${theme} @spooler-log`, async ({ browser, app, adminState }) => {
				const context = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height }, storageState: adminState });
				const page = await context.newPage();
				let release = () => {};
				try {
					release = await openStateRow(page, app, theme, state);
					const m = await measureStateRow(page);
					assert.equal(m.containerWidth < 640, viewport.card, `the container is ${m.containerWidth}px wide`);
					assertStateRow(m, viewport.card, `${state} ${viewport.name} ${theme}`);
				} finally {
					release();
					await context.close();
				}
			});
		}
	}
}

// Boundary: the card query is `width < 40rem`, so 639 px is card mode and 640 px is table mode.
// The inline width on the container is a measurement control only and is removed afterwards.
for (const theme of ["light", "dark"]) {
	test(`delivery queue log state row switches at the 40rem container boundary ${theme} @spooler-log`, async ({ browser, app, adminState }) => {
		const context = await browser.newContext({ viewport: { width: 1280, height: 720 }, storageState: adminState });
		const page = await context.newPage();
		try {
			await openStateRow(page, app, theme, "empty");
			const container = page.locator("#spooler-log-table").locator("xpath=ancestor::*[contains(@class,'ju-table-viewport')]");
			for (const [width, card] of [[639, true], [640, false]]) {
				await container.evaluate((el, w) => { el.style.width = `${w}px`; }, width);
				await expect.poll(() => page.evaluate(() => getComputedStyle(document.querySelector("#spooler-log-body td.spooler-log__state")).display), {
					message: `display at a ${width}px container`,
				}).toBe(card ? "block" : "table-cell");
				const m = await measureStateRow(page);
				assert.equal(m.containerWidth, width, "the container has the forced width");
				assertStateRow(m, card, `container ${width}px`);
			}
			await container.evaluate((el) => { el.style.removeProperty("width"); });
		} finally {
			await context.close();
		}
	});
}
