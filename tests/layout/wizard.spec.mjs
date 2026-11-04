//
// tests/layout/wizard.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { test as baseTest } from "./fixtures.mjs";

const test = baseTest.extend({
	storageState: async ({ adminState }, use) => {
		await use(adminState);
	},
});
// Check-step progressive disclosure of the target wizard: fields appear only for active checks.
const HTTP_FIELDS = ["#wizard-http-url", "#wizard-method", "#wizard-codes"];
const TLS_FIELDS = ["#wizard-ignore-cert"];
const TCP_FIELDS = ["#wizard-extra-ports"];
const TOGGLES = { http: "#wizard-http", ping: "#wizard-ping", tcp: "#wizard-tcp", cert: "#wizard-cert" };
const VIEWPORTS = [{ name: "390x844", width: 390, height: 844 }, { name: "320x640", width: 320, height: 640 }, { name: "1280x720", width: 1280, height: 720 }];

async function openChecksStep(page, app, { profile, address, ports }) {
	await page.goto(`${app.base}/ui/targets/new`);
	await page.locator(`input[name="profile"][value="${profile}"]`).check({ force: true });
	await page.getByRole("button", { name: "Continue" }).click();
	await page.locator("#wizard-address").fill(address);
	if (ports) await page.locator("#wizard-ports").fill(ports);
	await page.getByRole("button", { name: "Continue" }).click();
	await page.locator("#step-checks-title").waitFor({ state: "visible" });
	await page.locator("#wizard-advanced > summary").click();
}

async function reachableIds(page) {
	// Walk the whole tab order of the checks step and collect the ids that receive focus.
	await page.locator("#step-checks-title").focus();
	const ids = new Set();
	for (let index = 0; index < 40; index += 1) {
		await page.keyboard.press("Tab");
		const id = await page.evaluate(() => document.activeElement?.id || "");
		if (id) ids.add(id);
		if (id === "wizard-next") break;
	}
	return ids;
}


test("TCP target: HTTP and TLS fields are not rendered or reachable, the port field is @wizard", async ({ app, page }) => {
	await openChecksStep(page, app, { profile: "tcp", address: "tcp.example.org", ports: "22,443" });
	assert.equal(await page.locator("#wizard-tcp").isChecked(), true);
	assert.equal(await page.locator("#wizard-http").isChecked(), false);
	for (const selector of [...HTTP_FIELDS, ...TLS_FIELDS]) {
		assert.equal(await page.locator(selector).isHidden(), true, `${selector} must be hidden`);
	}
	for (const selector of TCP_FIELDS) assert.equal(await page.locator(selector).isVisible(), true, `${selector} must be visible`);
	assert.equal(await page.locator("#wizard-extra-ports").inputValue(), "22,443");
	const reachable = await reachableIds(page);
	for (const selector of [...HTTP_FIELDS, ...TLS_FIELDS]) {
		assert.equal(reachable.has(selector.slice(1)), false, `${selector} must not be in the tab order`);
	}
	assert.equal(reachable.has("wizard-extra-ports"), true, "the port field is keyboard reachable");
	// Interval, retries, retention and auto-close stay available.
	for (const id of ["wizard-interval", "wizard-retries", "wizard-retention", "wizard-auto-close"]) {
		assert.equal(reachable.has(id), true, `${id} stays reachable`);
	}
});


test("toggles show and hide their field groups, with keyboard and announcement @wizard", async ({ app, page }) => {
	await openChecksStep(page, app, { profile: "tcp", address: "toggle.example.org", ports: "443" });
	await page.locator(TOGGLES.http).focus();
	await page.keyboard.press("Space");
	for (const selector of [...HTTP_FIELDS, ...TLS_FIELDS]) {
		assert.equal(await page.locator(selector).isVisible(), true, `${selector} visible with HTTP`);
	}
	assert.match(await page.locator("#wizard-checks-status").innerText(), /HTTP/i);
	assert.equal(await page.evaluate(() => document.activeElement?.id), "wizard-http", "focus stays on the toggle");
	await page.keyboard.press("Space");
	for (const selector of [...HTTP_FIELDS, ...TLS_FIELDS]) {
		assert.equal(await page.locator(selector).isHidden(), true, `${selector} hidden again`);
	}
	for (let mask = 0; mask < 16; mask += 1) {
		const state = { http: Boolean(mask & 1), ping: Boolean(mask & 2), tcp: Boolean(mask & 4), cert: Boolean(mask & 8) };
		for (const [key, selector] of Object.entries(TOGGLES)) await page.locator(selector).setChecked(state[key], { force: true });
		for (const selector of HTTP_FIELDS) assert.equal(await page.locator(selector).isVisible(), state.http, `${selector} for mask ${mask}`);
		for (const selector of TLS_FIELDS) assert.equal(await page.locator(selector).isVisible(), state.http || state.cert, `${selector} for mask ${mask}`);
		for (const selector of TCP_FIELDS) assert.equal(await page.locator(selector).isVisible(), state.tcp, `${selector} for mask ${mask}`);
	}
});

test("the review shows only values of active checks @wizard", async ({ app, page }) => {
	await openChecksStep(page, app, { profile: "tcp", address: "review.example.org", ports: "22" });
	await page.getByRole("button", { name: "Continue" }).click();
	await page.locator("#step-recipients-title").waitFor({ state: "visible" });
	await page.getByRole("button", { name: "Continue" }).click();
	const summary = await page.locator("#wizard-summary").innerText();
	assert.doesNotMatch(summary, /HTTP method|Ignore certificate errors|Full URL/i);
	assert.match(summary, /TCP ports/i);
});

for (const viewport of VIEWPORTS) {
	for (const theme of ["light", "dark"]) {
		test(`checks step fits ${viewport.name} in ${theme} with HTTP and TCP active @wizard`, async ({ app, page }) => {
			await page.setViewportSize({ width: viewport.width, height: viewport.height });
			await openChecksStep(page, app, { profile: "custom", address: "https://fit.example.org/", ports: "443" });
			await page.evaluate((value) => document.documentElement.setAttribute("data-bs-theme", value), theme);
			await page.locator(TOGGLES.tcp).setChecked(true, { force: true });
			await page.locator(TOGGLES.http).setChecked(true, { force: true });
			for (const selector of [...HTTP_FIELDS, ...TCP_FIELDS]) {
				const box = await page.locator(selector).boundingBox();
				assert.ok(box && box.x >= 0 && box.x + box.width <= viewport.width + 1, `${selector} inside the ${viewport.name} viewport`);
			}
			assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), true, "no horizontal page overflow");
		});
	}
}
