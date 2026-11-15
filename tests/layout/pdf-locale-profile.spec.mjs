//
// tests/layout/pdf-locale-profile.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// The browser supplies the active Intl conventions to a PDF job without placing Unicode in Fetch headers.
import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "./fixtures.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");

test("PDF export sends an ASCII-safe Intl profile for each browser locale @downtime-widget-link", async ({ browser, app }) => {
	for (const locale of ["de-DE", "en-US", "fr-FR", "hi-IN"]) {
		const context = await browser.newContext({ locale, viewport: { width: 1280, height: 800 } });
		const page = await context.newPage();
		const profiles = [];
		await page.route("**/reports/targets/1/pdf/request**", async (route) => {
			profiles.push(route.request().headers()["x-justup-locale-profile"]);
			await route.fulfill({
				status: 200,
				contentType: "application/json",
				body: JSON.stringify({ job_id: "locale-job", status: "pending" }),
			});
		});
		try {
			await page.goto(`${app.base}/ui/login`, { waitUntil: "load" });
			await page.evaluate(() => { window.withCsrf = (options) => options; });
			await page.addScriptTag({ path: path.join(ROOT, "app/static/js/app.js") });
			await page.evaluate(() => {
				document.body.insertAdjacentHTML("beforeend", `
					<button id="export-pdf-btn"><span id="pdf-icon"></span><span id="pdf-spinner"></span></button>
					<span id="pdf-export-status" hidden></span>`);
				window.appState = { range: { from: null, to: null } };
				window.justUpPdfJobs = { add() {} };
				if (typeof window.apiFetch !== "function") throw new Error("apiFetch did not load");
			});
			await page.addScriptTag({ path: path.join(ROOT, "app/static/js/target-detail/pdf-export.js") });
			const request = page.waitForRequest((candidate) => candidate.url().includes("/reports/targets/1/pdf/request"));
			await page.evaluate(() => window.downloadPdfReport(1, "a4"));
			await request;
			assert.equal(profiles.length, 1, `${locale}: one request`);
			const encoded = profiles[0];
			assert.ok(encoded && [...encoded].every((char) => char.charCodeAt(0) < 128), `${locale}: ASCII-safe header`);
			const profile = JSON.parse(decodeURIComponent(encoded));
			assert.equal(profile.locale, locale);
			assert.ok(profile.dates.fullDateTime.some((part) => part.type === "hour" && part.pad), `${locale}: padded time`);
			if (locale === "de-DE") assert.equal(profile.number.decimal, ",");
			if (locale === "en-US") assert.equal(profile.number.decimal, ".");
			if (locale === "fr-FR") assert.equal(profile.number.group, "\u202f");
			if (locale === "hi-IN") assert.deepEqual(profile.number.integerGroups.slice(-3), [2, 2, 3]);
		} finally {
			await context.close();
		}
	}
});
