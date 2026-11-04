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
const TCP_FIELDS = ["#wizard-tcp-summary"];
const TOGGLES = { http: "#wizard-http", ping: "#wizard-ping", tcp: "#wizard-tcp", cert: "#wizard-cert" };
const VIEWPORTS = [{ name: "390x844", width: 390, height: 844 }, { name: "320x640", width: 320, height: 640 }, { name: "1280x720", width: 1280, height: 720 }];
const SCAN_ID = "123e4567-e89b-12d3-a456-426614174000";

async function stubPortScan(page) {
	await page.route("**/network-scans**", async route => {
		const method = route.request().method();
		if (method === "DELETE") return route.fulfill({ status: 204 });
		const job = {
			id: SCAN_ID, host: "mail.example.org", protocols: ["tcp", "udp"],
			status: method === "POST" ? "queued" : "completed",
			result: method === "POST" ? null : {
				hosts: [{ address: "mail.example.org", status: "up", ports: [
					{ protocol: "tcp", port: 443, state: "open", service: { name: "https", method: "probed" } },
					{ protocol: "udp", port: 53, state: "open", service: { name: "domain", method: "probed" } },
					{ protocol: "udp", port: 161, state: "open|filtered", service: { name: "snmp", method: "table" } },
				] }], complete: true,
			}
		};
		return route.fulfill({ status: method === "POST" ? 202 : 200, contentType: "application/json", body: JSON.stringify(job) });
	});
}

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
	assert.equal(await page.locator("#wizard-group-tcp input[type=text]").count(), 0, "step 3 has no second port input");
	assert.equal(await page.locator("#wizard-extra-ports").count(), 0);
	assert.match(await page.locator("#wizard-tcp-summary").innerText(), /TCP ports:\s*22, 443/);
	const reachable = await reachableIds(page);
	for (const selector of [...HTTP_FIELDS, ...TLS_FIELDS]) {
		assert.equal(reachable.has(selector.slice(1)), false, `${selector} must not be in the tab order`);
	}
	assert.equal(await page.getByRole("button", { name: "Edit ports" }).first().isVisible(), true);
	// Interval, retries, retention and auto-close stay available.
	for (const id of ["wizard-interval", "wizard-retries", "wizard-retention", "wizard-auto-close"]) {
		assert.equal(reachable.has(id), true, `${id} stays reachable`);
	}
});


test("ports are entered once: the checks step shows a summary and an Edit ports action @wizard", async ({ app, page }) => {
	await openChecksStep(page, app, { profile: "tcp", address: "once.example.org", ports: "25" });
	assert.match(await page.locator("#wizard-tcp-summary").innerText(), /TCP ports:\s*25/);
	await page.getByRole("button", { name: "Edit ports" }).click();
	await page.locator("#step-address-title").waitFor({ state: "visible" });
	assert.equal(await page.evaluate(() => document.activeElement?.id), "wizard-ports", "the source field is focused");
	await page.locator("#wizard-ports").fill("25,587");
	await page.getByRole("button", { name: "Continue" }).click();
	await page.locator("#step-checks-title").waitFor({ state: "visible" });
	assert.match(await page.locator("#wizard-tcp-summary").innerText(), /TCP ports:\s*25, 587/);
	// Back keeps working and the ports survive the round trip.
	await page.getByRole("button", { name: "Back" }).click();
	assert.equal(await page.locator("#wizard-ports").inputValue(), "25,587");
});

test("TCP on without a port blocks Continue with an inline error and a jump action @wizard", async ({ app, page }) => {
	await openChecksStep(page, app, { profile: "custom", address: "none.example.org" });
	await page.locator(TOGGLES.tcp).setChecked(true, { force: true });
	assert.match(await page.locator("#wizard-tcp-summary").innerText(), /None chosen yet/);
	assert.equal(await page.locator("#wizard-ports").inputValue(), "", "no port is invented");
	await page.getByRole("button", { name: "Continue" }).click();
	assert.match(await page.locator("#wizard-tcp-summary-error").innerText(), /Choose at least one TCP port\./);
	assert.equal(await page.locator("#step-checks-title").isVisible(), true, "still on the checks step");
	assert.equal(await page.evaluate(() => document.activeElement?.dataset.editPorts), "tcp");
	await page.getByRole("button", { name: "Edit ports" }).click();
	assert.equal(await page.evaluate(() => document.activeElement?.id), "wizard-ports");
});

test("a port check on a website profile reveals the port fields from Edit ports @wizard", async ({ app, page }) => {
	await openChecksStep(page, app, { profile: "website", address: "https://site.example.org/" });
	await page.locator(TOGGLES.tcp).setChecked(true, { force: true });
	await page.getByRole("button", { name: "Edit ports" }).click();
	assert.equal(await page.locator("#wizard-ports").isVisible(), true);
	assert.equal(await page.evaluate(() => document.activeElement?.id), "wizard-ports");
});

test("the submitted payload keeps tcp_ports and udp_ports from the address step @wizard", async ({ app, page }) => {
	await page.goto(`${app.base}/ui/targets/new`);
	await page.locator('input[name="profile"][value="custom"]').check({ force: true });
	await page.getByRole("button", { name: "Continue" }).click();
	await page.locator("#wizard-address").fill("payload.example.org");
	await page.locator("#wizard-ports").fill("22,443");
	await page.locator("#wizard-udp-ports").fill("53");
	await page.getByRole("button", { name: "Continue" }).click();
	await page.locator("#step-checks-title").waitFor({ state: "visible" });
	await page.locator(TOGGLES.tcp).setChecked(true, { force: true });
	await page.locator("#wizard-udp").setChecked(true, { force: true });
	await page.getByRole("button", { name: "Continue" }).click();
	await page.locator("#step-recipients-title").waitFor({ state: "visible" });
	await page.getByRole("button", { name: "Continue" }).click();
	let body = null;
	await page.route("**/targets", async route => {
		if (route.request().method() !== "POST") return route.fallback();
		body = route.request().postDataJSON();
		return route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "stop" }) });
	});
	await page.getByRole("button", { name: "Start monitoring" }).click();
	await page.locator("#wizard-error:not(.d-none)").waitFor();
	assert.equal(body.tcp_ports, "22,443");
	assert.equal(body.udp_ports, "53");
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

test("discovered TCP and UDP ports can be selected without saving the wizard @wizard", async ({ app, page }) => {
	await stubPortScan(page);
	await page.goto(`${app.base}/ui/targets/new`);
	await page.locator('input[name="profile"][value="custom"]').check({ force: true });
	await page.getByRole("button", { name: "Continue" }).click();
	await page.locator("#wizard-address").fill("mail.example.org");
	await page.locator("#wizard-ports").fill("22");
	await page.locator('[data-scan-protocol="udp"]').check();
	await page.getByRole("button", { name: "Scan ports" }).click();
	await page.locator("[data-scan-results]").waitFor({ state: "visible" });
	await page.getByRole("button", { name: "Select all confirmed open ports" }).click();
	assert.equal(await page.locator('[data-scan-rows] input:checked').count(), 2, "uncertain UDP is not selected as open");
	await page.getByRole("checkbox", { name: "Select UDP port 161 (open|filtered)" }).check();
	await page.getByRole("button", { name: "Add selected ports" }).click();
	assert.equal(await page.locator("#wizard-ports").inputValue(), "22,443");
	assert.equal(await page.locator("#wizard-udp-ports").inputValue(), "53,161");
	assert.equal(await page.locator("#wizard-tcp").isChecked(), true);
	assert.equal(await page.locator("#wizard-udp").isChecked(), true);
	await page.getByRole("button", { name: "Continue" }).click();
	await page.locator("#step-checks-title").waitFor({ state: "visible" });
	assert.match(await page.locator("#wizard-tcp-summary").innerText(), /22, 443/);
	assert.match(await page.locator("#wizard-udp-summary").innerText(), /53, 161/);
	await page.getByRole("button", { name: "Back" }).click();
	assert.equal(await page.locator('[name="network_scan_id"]').inputValue(), SCAN_ID);
});

test("discovered ports preserve manual entries in target edit @wizard", async ({ app, page }) => {
	await stubPortScan(page);
	await page.goto(`${app.base}/ui/targets/1/edit`);
	// The host lives on the General tab; the port settings on the Network tab.
	await page.locator("#host-input").fill("mail.example.org");
	await page.locator("#target-network-tab").click();
	await page.locator("#enable_tcp_connect").check();
	await page.locator("#tcp_ports_input").fill("22");
	await page.locator("#tcp_ports_input").press("Enter");
	await page.locator("#enable_udp_scan").check();
	await page.locator("#udp_ports").fill("161");
	await page.locator('[data-scan-protocol="udp"]').check();
	await page.getByRole("button", { name: "Scan ports" }).click();
	await page.locator("[data-scan-results]").waitFor({ state: "visible" });
	await page.getByRole("button", { name: "Select all confirmed open ports" }).click();
	await page.getByRole("button", { name: "Add selected ports" }).click();
	assert.match(await page.locator("#tcp_ports").inputValue(), /(?:^|,)22(?:,|$)/);
	assert.match(await page.locator("#tcp_ports").inputValue(), /(?:^|,)443(?:,|$)/);
	assert.equal(await page.locator("#udp_ports").inputValue(), "53,161");
	assert.equal(await page.locator('[name="network_scan_id"]').inputValue(), SCAN_ID);
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
// Port discovery needs the Nmap worker: while it is down the scan cannot start and the reason is on screen.
const WORKER_DOWN = { available: false, state: "unavailable", message: "Nmap scan worker is unavailable", detail: "" };
const WORKER_UP = { available: true, state: "ready", message: "", detail: "" };
async function stubWorkerStatus(page, worker) {
	// Registered after stubPortScan so it wins for /network-scans/status.
	await page.route("**/network-scans/status", route => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(worker.current) }));
}
function countScanStarts(page) {
	const starts = { count: 0 };
	page.on("request", request => { if (request.method() === "POST" && request.url().endsWith("/network-scans")) starts.count += 1; });
	return starts;
}
async function openScanStep(page, app, address = "mail.example.org") {
	await page.goto(`${app.base}/ui/targets/new`);
	await page.locator('input[name="profile"][value="tcp"]').check({ force: true });
	await page.getByRole("button", { name: "Continue" }).click();
	await page.locator("#wizard-address").fill(address);
	await page.locator("#wizard-ports").fill("22,443");
}
test("wizard: scan button is disabled with a visible, announced reason while the worker is down, manual ports still save @wizard", async ({ app, page }) => {
	const worker = { current: WORKER_DOWN };
	await stubPortScan(page);
	await stubWorkerStatus(page, worker);
	const starts = countScanStarts(page);
	await openScanStep(page, app);
	const scan = page.getByRole("button", { name: "Scan ports" });
	await page.locator('[data-scan-action="start"]:disabled').waitFor();
	const notice = page.locator("[data-scan-unavailable]");
	assert.equal(await notice.isVisible(), true, "the reason is visible without clicking");
	assert.match(await notice.innerText(), /Nmap scan worker is unavailable.*enter ports manually/is);
	const describedBy = await scan.getAttribute("aria-describedby");
	assert.ok(describedBy, "the disabled button names its reason");
	assert.match(await page.locator(`#${describedBy}`).innerText(), /Nmap scan worker is unavailable/);
	assert.equal(await notice.evaluate(element => element.closest('[role="status"]') !== null), true, "the reason sits in a live region");
	await scan.click({ force: true });
	assert.equal(starts.count, 0, "a disabled button starts no scan");
	// Protocols and scope stay usable and manual entry is unaffected.
	await page.locator('[data-scan-protocol="udp"]').check();
	await page.locator("[data-scan-profile]").selectOption("extended");
	await page.locator("#wizard-udp-ports").fill("161");
	await page.getByRole("button", { name: "Continue" }).click();
	await page.locator("#step-checks-title").waitFor({ state: "visible" });
	await page.getByRole("button", { name: "Continue" }).click();
	await page.locator("#step-recipients-title").waitFor({ state: "visible" });
	await page.getByRole("button", { name: "Continue" }).click();
	let body = null;
	await page.route("**/targets", async route => {
		if (route.request().method() !== "POST") return route.fallback();
		body = route.request().postDataJSON();
		return route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "stop" }) });
	});
	await page.getByRole("button", { name: "Start monitoring" }).click();
	await page.locator("#wizard-error:not(.d-none)").waitFor();
	assert.equal(body.tcp_ports, "22,443", "manual TCP ports are saved without any scan");
	assert.equal(body.udp_ports, "161", "the manual UDP port is saved without any scan");
});
test("wizard: scan button is enabled and no reason is shown while the worker is available @wizard", async ({ app, page }) => {
	await stubPortScan(page);
	await stubWorkerStatus(page, { current: WORKER_UP });
	const statusSeen = page.waitForResponse("**/network-scans/status");
	await openScanStep(page, app);
	await statusSeen;
	assert.equal(await page.getByRole("button", { name: "Scan ports" }).isEnabled(), true);
	assert.equal(await page.locator("[data-scan-unavailable]").isHidden(), true);
	assert.equal(await page.getByRole("button", { name: "Scan ports" }).getAttribute("aria-describedby"), null);
});
test("wizard: the button follows the worker back and forth via Check again and focus @wizard", async ({ app, page }) => {
	const worker = { current: WORKER_DOWN };
	await stubPortScan(page);
	await stubWorkerStatus(page, worker);
	await openScanStep(page, app);
	const scan = page.getByRole("button", { name: "Scan ports" });
	await page.locator('[data-scan-action="start"]:disabled').waitFor();
	worker.current = WORKER_UP;
	await page.getByRole("button", { name: "Check again" }).click();
	await page.locator('[data-scan-action="start"]:enabled').waitFor();
	assert.equal(await page.locator("[data-scan-unavailable]").isHidden(), true);
	assert.equal(await scan.getAttribute("aria-describedby"), null);
	assert.match(await page.locator("[data-scan-status]").innerText(), /available again/i);
	// Down again; regaining focus re-checks once the 5 s throttle has passed.
	worker.current = WORKER_DOWN;
	await page.waitForTimeout(5200);
	await page.evaluate(() => window.dispatchEvent(new Event("focus")));
	await page.locator('[data-scan-action="start"]:disabled').waitFor();
	assert.equal(await page.locator("[data-scan-unavailable]").isVisible(), true);
});
test("wizard: a refused scan start disables the button with the server reason @wizard", async ({ app, page }) => {
	await stubPortScan(page);
	await stubWorkerStatus(page, { current: WORKER_UP });
	await page.route("**/network-scans", async route => {
		if (route.request().method() !== "POST") return route.fallback();
		return route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: { code: "unavailable", message: "Nmap scan worker is restarting" } }) });
	});
	const statusSeen = page.waitForResponse("**/network-scans/status");
	await openScanStep(page, app);
	await statusSeen;
	await page.getByRole("button", { name: "Scan ports" }).click();
	await page.locator('[data-scan-action="start"]:disabled').waitFor();
	assert.match(await page.locator("[data-scan-unavailable]").innerText(), /Nmap scan worker is restarting/);
});
test("target edit: scan button is disabled while the worker is down, manual ports still save @wizard", async ({ app, page }) => {
	await stubPortScan(page);
	await stubWorkerStatus(page, { current: WORKER_DOWN });
	const starts = countScanStarts(page);
	await page.goto(`${app.base}/ui/targets/1/edit`);
	await page.locator("#host-input").fill("mail.example.org");
	await page.locator("#target-network-tab").click();
	await page.locator('[data-scan-action="start"]:disabled').waitFor();
	assert.equal(await page.locator("[data-scan-unavailable]").isVisible(), true);
	const describedBy = await page.getByRole("button", { name: "Scan ports" }).getAttribute("aria-describedby");
	assert.match(await page.locator(`#${describedBy}`).innerText(), /Nmap scan worker is unavailable/);
	await page.locator("#enable_tcp_connect").check();
	await page.locator("#tcp_ports_input").fill("22");
	await page.locator("#tcp_ports_input").press("Enter");
	await page.locator("#enable_udp_scan").check();
	await page.locator("#udp_ports").fill("161");
	let body = null;
	await page.route("**/targets/1", async route => {
		if (route.request().method() === "GET") return route.fallback();
		body = route.request().postDataJSON();
		return route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "stop" }) });
	});
	const saved = page.waitForRequest(request => request.url().endsWith("/targets/1") && request.method() !== "GET");
	await page.locator('#target-form button[type="submit"]').click();
	await saved;
	assert.match(body.tcp_ports, /(?:^|,)22(?:,|$)/);
	assert.equal(body.udp_ports, "161");
	assert.equal(starts.count, 0);
});
