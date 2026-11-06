//
// tests/layout/recipients-search.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Settings > Recipients search: filters while typing with the shared list-filter core, Clear/Escape,
// empty state, live region, working row actions and a surviving query after a reload
// (DESIGN.md, "Filter-as-you-type search").
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test as base } from "@playwright/test";
import { recipientScripts, startStaticAppServer } from "./test-support.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const TEMPLATES = path.join(ROOT, "app", "templates", "settings");
const SCRIPTS = recipientScripts({ listFilter: true });
const strip = (file) => readFileSync(path.join(TEMPLATES, file), "utf8").replace(/\{#[\s\S]*?#\}/g, "");
const PAGE = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/static/css/bootstrap.min.css"><link rel="stylesheet" href="/static/css/material-icons.css"><link rel="stylesheet" href="/static/css/gentelella-layout.css"><link rel="stylesheet" href="/static/css/ui.css">
</head><body><main class="ui-page" id="host">${strip("recipients.html")}</main>${strip("modals.html")}
${SCRIPTS.map((src) => `<script src="/static/js/${src}"></script>`).join("")}</body></html>`;
const VIEWPORTS = [
	{ name: "1280x720", width: 1280, height: 720 },
	{ name: "390x844", width: 390, height: 844 },
	{ name: "320x640", width: 320, height: 640 },
	{ name: "200pct-zoom", width: 640, height: 360 },
];
const PEOPLE = [
	{ id: 1, name: "Martin", email: "martin@example.com", phone: "", is_enabled: true },
	{ id: 2, name: "Tobias", email: "", phone: "+4915112345678", is_enabled: true },
	{ id: 3, name: "Ünal Özdemir", email: "unal@example.org", phone: "", is_enabled: false },
];
const test = base.extend({
	base: [async ({}, use) => {
		const server = await startStaticAppServer({ root: ROOT, renderPage: () => PAGE });
		try {
			await use(server.baseUrl);
		} finally {
			await server.close();
		}
	}, { scope: "worker" }],
});
const visibleNames = (page) => page.evaluate(() => [...document.querySelectorAll("#recipients-list tbody tr")]
	.filter((row) => !row.hidden).map((row) => row.querySelector("strong").textContent.trim()));

for (const viewport of VIEWPORTS) {
	for (const theme of ["light", "dark"]) {
		test(`Recipients search filters, resets and survives a reload at ${viewport.name} ${theme} @recipients-search`, async ({ page, base }) => {
			const errors = [];
			page.on("pageerror", (e) => errors.push(String(e)));
			page.on("console", (m) => { if (m.type() === "error" && !m.text().startsWith("Failed to load resource")) errors.push(m.text()); });
			await page.setViewportSize({ width: viewport.width, height: viewport.height });
			await page.goto(base);
			await page.evaluate(async ({ t, people }) => {
				document.documentElement.setAttribute("data-bs-theme", t);
				window.__calls = [];
				const respond = (body) => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
				window.apiFetch = (url, options) => {
					window.__calls.push(`${(options && options.method) || "GET"} ${url}`);
					return respond(url === "/recipients" ? people : { results: [] });
				};
				await window.SettingsPage.Recipients.loadRecipients();
			}, { t: theme, people: PEOPLE });
			await page.waitForSelector("#recipients-list tbody tr");
			const status = () => page.textContent("#recipients-search-status");
			const emptyHidden = () => page.evaluate(() => document.getElementById("recipients-search-empty").hidden);

			assert.deepEqual(await visibleNames(page), ["Martin", "Tobias", "Ünal Özdemir"]);
			assert.equal(await page.evaluate(() => document.getElementById("recipients-search-clear").hidden), true, "clear hidden while empty");

			// Typing filters at once; name, address and number are searched, case and diacritics fold.
			await page.fill("#recipients-search", "  MARTIN ");
			assert.deepEqual(await visibleNames(page), ["Martin"]);
			assert.equal(await status(), "1 recipient found");
			await page.fill("#recipients-search", "unal");
			assert.deepEqual(await visibleNames(page), ["Ünal Özdemir"]);
			await page.fill("#recipients-search", "151123");
			assert.deepEqual(await visibleNames(page), ["Tobias"]);
			await page.fill("#recipients-search", "example");
			assert.equal(await status(), "2 recipients found");

			// A hit keeps its actions: Send test works for the filtered row.
			await page.fill("#recipients-search", "tobias");
			await page.click('#recipients-list tr:not([hidden]) .test-recipient-btn');
			await page.waitForFunction(() => window.__calls.some((c) => c.startsWith("POST /recipients/2/test")), null, { timeout: 5000 });

			// No hit: empty state with the term and a reset, no table frame left, live region says so.
			await page.fill("#recipients-search", "zzz");
			assert.equal(await emptyHidden(), false);
			assert.equal(await page.textContent("#recipients-search-term"), '"zzz"');
			assert.equal(await status(), "No recipients found");
			assert.equal(await page.evaluate(() => document.querySelector("#recipients-list table").hidden), true);

			// A reload (save, delete, pause) re-applies the query to the new rows.
			await page.evaluate(() => window.SettingsPage.Recipients.loadRecipients());
			await page.fill("#recipients-search", "tobias");
			await page.evaluate(() => window.SettingsPage.Recipients.loadRecipients());
			await page.waitForFunction(() => document.querySelectorAll("#recipients-list tbody tr[data-search]").length === 3);
			assert.deepEqual(await visibleNames(page), ["Tobias"], "query survives the reload");
			assert.equal(await page.inputValue("#recipients-search"), "tobias");

			// Escape and the buttons reset and keep focus in the field.
			await page.focus("#recipients-search");
			await page.keyboard.press("Escape");
			assert.equal(await page.inputValue("#recipients-search"), "");
			assert.equal(await page.evaluate(() => document.activeElement.id), "recipients-search");
			assert.equal((await visibleNames(page)).length, 3);
			await page.fill("#recipients-search", "zzz");
			await page.click("#recipients-search-reset");
			assert.equal(await emptyHidden(), true);
			assert.equal((await visibleNames(page)).length, 3);
			await page.fill("#recipients-search", "mart");
			await page.click("#recipients-search-clear");
			assert.equal(await page.inputValue("#recipients-search"), "");
			assert.equal(await status(), "");

			// Nothing overflows; the field never leaves the card.
			const geometry = await page.evaluate(() => {
				const de = document.documentElement;
				const box = document.querySelector(".ui-search").getBoundingClientRect();
				const card = document.querySelector(".settings-editor").getBoundingClientRect();
				return { overflow: de.scrollWidth > de.clientWidth + 1, inside: box.left >= card.left - 1 && box.right <= card.right + 1, width: box.width };
			});
			assert.equal(geometry.overflow, false, "page overflows horizontally");
			assert.equal(geometry.inside, true, "search field leaves the card");
			assert.ok(geometry.width <= 28 * 16 + 1, "field wider than the shared maximum");
			assert.deepEqual(errors, []);
		});
	}
}
