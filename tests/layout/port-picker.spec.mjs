//
// tests/layout/port-picker.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Port picker of the target wizard and the target form: manual and protocol-typed entry, collapsed Nmap discovery,
// confirmed-first results, preselection, adoption, error states, stale answers and safe rendering. The scan API is stubbed; the
// worker itself is covered by the backend tests. Every scenario ends without saving unless it says so.
import assert from "node:assert/strict";
import { test as baseTest } from "./fixtures.mjs";
const test = baseTest.extend({
	storageState: async ({ adminState }, use) => {
		await use(adminState);
	},
});
test.setTimeout(90_000);
const SCAN_ID = "123e4567-e89b-12d3-a456-426614174000";
const OTHER_ID = "223e4567-e89b-12d3-a456-426614174000";
const PORTS = [
	{ protocol: "tcp", port: 22, state: "open", service: { name: "ssh", product: "OpenSSH", version: "9.2", method: "probed" } },
	{ protocol: "tcp", port: 443, state: "open", service: { name: "https", method: "probed" } },
	{ protocol: "udp", port: 53, state: "open", service: { name: "domain", method: "probed" } },
	{ protocol: "udp", port: 161, state: "open|filtered", service: { name: "snmp", method: "table" } },
];
// 16 results, one confirmed open: the case the compact results exist for.
const SIXTEEN = [
	{ protocol: "tcp", port: 80, state: "open", service: { name: "http", method: "table" } },
	{ protocol: "tcp", port: 21, state: "closed" },
	{ protocol: "tcp", port: 23, state: "closed" },
	...[111, 135, 139, 445, 1433, 3306, 3389, 5900, 8080, 8443, 9100, 9200].map((port) => ({ protocol: "tcp", port, state: "filtered" })),
	{ protocol: "udp", port: 161, state: "open|filtered", service: { name: "snmp", method: "table" } },
];
const job = (status, extra = {}) => ({ id: SCAN_ID, host: "mail.example.org", protocols: ["tcp", "udp"], profile: "quick", status, ...extra });
const completed = (ports = PORTS, id = SCAN_ID) => job("completed", { id, result: { hosts: [{ address: "192.0.2.10", status: "up", ports }], complete: true } });
const failed = (code, message = "server text") => job("failed", { error: { code, message } });
// Registers the scan API stub. Later routes win, so /status is registered last.
async function stubScan(page, overrides = {}) {
	const log = { starts: [], polls: [], deletes: [] };
	const state = {
		worker: { available: true, state: "ready", message: "", detail: "" },
		start: async () => ({ status: 202, json: job("queued", { result: null }) }),
		poll: async () => ({ status: 200, json: completed() }),
		...overrides,
	};
	const answer = async (route, result) => {
		try {
			await route.fulfill({ status: result.status, contentType: "application/json", body: JSON.stringify(result.json ?? {}) });
		} catch {
			// The page aborted the request (cancel, host change); a stale answer is simply dropped.
		}
	};
	await page.route(/\/network-scans(\/[^/?]+)?$/, async (route) => {
		const request = route.request();
		const id = request.url().split("/").pop();
		if (request.method() === "POST") {
			log.starts.push(request.postDataJSON());
			return answer(route, await state.start(request.postDataJSON()));
		}
		if (request.method() === "DELETE") {
			log.deletes.push(id);
			return route.fulfill({ status: 204 });
		}
		log.polls.push(id);
		return answer(route, await state.poll(id));
	});
	await page.route("**/network-scans/status", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(state.worker) }));
	return { log, state };
}
// Nmap discovery is collapsed by default; tests that scan open it first.
async function openDiscover(picker) {
	const details = picker.locator("[data-scan-discover]");
	if (!(await details.evaluate((node) => node.open))) await details.locator("summary").click();
}
async function openWizard(page, app, address = "mail.example.org", { discover = true } = {}) {
	await page.goto(`${app.base}/ui/targets/new`);
	await page.locator('input[name="profile"][value="custom"]').check({ force: true });
	await page.getByRole("button", { name: "Continue" }).click();
	await page.locator("#wizard-address").fill(address);
	const picker = page.locator("[data-network-scan-picker]");
	await picker.waitFor();
	if (discover) await openDiscover(picker);
	return picker;
}
const scan = (picker) => picker.getByRole("button", { name: "Scan ports", exact: true });
const chips = (picker) => picker.locator("[data-port-chips] .tag-input__text").allInnerTexts();
async function runScan(picker) {
	await scan(picker).click();
	await picker.locator("[data-scan-results]").waitFor({ state: "visible" });
}
async function addManual(picker, text) {
	await picker.locator("[data-manual-input]").fill(text);
	await picker.getByRole("button", { name: "Add ports" }).click();
}
async function message(picker) {
	const error = picker.locator("[data-scan-error]");
	if (await error.isVisible()) return `error: ${(await error.innerText()).trim()}`;
	return `status: ${(await picker.locator("[data-scan-status-text]").innerText()).trim()}`;
}
async function rowOf(picker, protocol, port) {
	return picker.locator("[data-scan-results] li", { has: picker.page().locator(`input[data-protocol="${protocol}"][data-port="${port}"]`) });
}
// Row text without the Material Icons ligature names.
const rowText = async (picker, protocol, port) => (await (await rowOf(picker, protocol, port)).innerText()).replace(/\b(?:check_circle|cancel|help)\s*/g, "");
const ticked = (picker) => picker.locator("[data-scan-results] input:checked").evaluateAll((nodes) => nodes.map((node) => `${node.dataset.protocol}:${node.dataset.port}`).sort());
const openOthers = async (picker) => {
	const other = picker.locator("[data-scan-other]");
	if (!(await other.evaluate((node) => node.open))) await other.locator("summary").click();
};
test("manual typed entry fills TCP and UDP, keeps chips removable and needs no scan @portpicker", async ({ app, page }) => {
	const { log } = await stubScan(page);
	const picker = await openWizard(page, app, "mail.example.org", { discover: false });
	// The wizard offers exactly one port input; wizard-ports and wizard-udp-ports are hidden backing fields.
	assert.deepEqual(
		await page.locator('[data-step="1"] :is(input, select, textarea):visible').evaluateAll((nodes) => nodes.map((node) => node.id)),
		["wizard-address", "wizard-name", "wizard-group", "wizard-manual-ports"],
	);
	assert.deepEqual(await page.locator("#wizard-ports, #wizard-udp-ports").evaluateAll((nodes) => nodes.map((node) => node.type)), ["hidden", "hidden"]);
	assert.equal(await picker.locator("[data-scan-discover]").evaluate((node) => node.open), false, "Nmap discovery starts collapsed");
	await addManual(picker, "22, 443 udp:53 161/udp");
	assert.equal(await page.locator("#wizard-ports").inputValue(), "22,443");
	assert.equal(await page.locator("#wizard-udp-ports").inputValue(), "53,161");
	assert.deepEqual(await chips(picker), ["TCP 22", "TCP 443", "UDP 53", "UDP 161"]);
	assert.equal(await page.locator("#wizard-tcp").isChecked(), true, "adding TCP ports switches the TCP check on");
	assert.equal(await page.locator("#wizard-udp").isChecked(), true, "adding UDP ports switches the UDP check on");
	// Invalid input changes nothing and names every bad token.
	await addManual(picker, "65536 foo udp:0 80");
	const problem = await picker.locator("[data-manual-error]").innerText();
	assert.match(problem, /65536/);
	assert.match(problem, /foo/);
	assert.match(problem, /udp:0/);
	assert.equal(await page.locator("#wizard-ports").inputValue(), "22,443");
	assert.equal(await picker.locator("[data-manual-input]").getAttribute("aria-invalid"), "true");
	// Duplicates are not added twice; both ends of the range are valid.
	await addManual(picker, "22 udp:53");
	assert.match(await message(picker), /already in the list/);
	await addManual(picker, "1 65535");
	assert.equal(await page.locator("#wizard-ports").inputValue(), "1,22,443,65535");
	// The same number on TCP and UDP stays two ports.
	await addManual(picker, "53");
	assert.deepEqual((await chips(picker)).filter((text) => text.endsWith(" 53")), ["TCP 53", "UDP 53"]);
	// Removal.
	await picker.getByRole("button", { name: "Remove UDP port 161" }).click();
	assert.equal(await page.locator("#wizard-udp-ports").inputValue(), "53");
	assert.equal((await chips(picker)).includes("UDP 161"), false);
	// Saving never depends on a scan: the checks step shows the ports and no scan was started.
	await page.getByRole("button", { name: "Continue" }).click();
	await page.locator("#step-checks-title").waitFor({ state: "visible" });
	assert.match(await page.locator("#wizard-tcp-summary").innerText(), /1, 22, 53, 443, 65535/);
	assert.match(await page.locator("#wizard-udp-summary").innerText(), /UDP ports:\s*53/);
	assert.equal(log.starts.length, 0);
});
test("protocols follow the active checks, ICMP is independent and HTTP is untouched @portpicker", async ({ app, page }) => {
	const { log } = await stubScan(page, { poll: async () => ({ status: 200, json: completed([]) }) });
	await openWizard(page, app, "mail.example.org");
	await page.getByRole("button", { name: "Continue" }).click();
	await page.locator("#step-checks-title").waitFor({ state: "visible" });
	assert.equal(await page.locator("#wizard-ping").isChecked(), true, "a host target starts with ICMP");
	assert.equal(await page.locator("#wizard-http").isChecked(), false);
	await page.locator("#wizard-udp").setChecked(true, { force: true });
	await page.locator("#wizard-tcp").setChecked(true, { force: true });
	for (const id of ["wizard-ping", "wizard-tcp", "wizard-udp"]) assert.equal(await page.locator(`#${id}`).isChecked(), true, id);
	assert.equal(await page.locator("#wizard-http").isChecked(), false, "TCP and UDP leave HTTP and certificate monitoring alone");
	assert.equal(await page.locator("#wizard-cert").isChecked(), false);
	await page.locator("#wizard-tcp").setChecked(false, { force: true });
	assert.equal(await page.locator("#wizard-udp").isChecked(), true, "UDP stays on without TCP");
	assert.equal(await page.locator("#wizard-ping").isChecked(), true, "ICMP stays on");
	await page.locator("#wizard-ping").setChecked(false, { force: true });
	assert.equal(await page.locator("#wizard-udp").isChecked(), true, "ICMP off does not touch UDP");
	// The scan asks for exactly the protocols of the active checks.
	await page.getByRole("button", { name: "Edit ports" }).first().click();
	const picker = page.locator("[data-network-scan-picker]");
	await openDiscover(picker);
	assert.equal(await picker.locator('[data-scan-protocol="udp"]').isChecked(), true);
	assert.equal(await picker.locator('[data-scan-protocol="tcp"]').isChecked(), false);
	await scan(picker).click();
	await picker.locator("[data-scan-status-text]", { hasText: /No open ports/ }).waitFor();
	assert.deepEqual(log.starts, [{ host: "mail.example.org", protocols: ["udp"], profile: "quick" }]);
	await picker.locator('[data-scan-protocol="udp"]').uncheck();
	await scan(picker).click();
	assert.match(await message(picker), /error: Choose TCP or UDP/);
	assert.equal(log.starts.length, 1, "no request without a protocol");
});
test("scan, confirmed-first results, selection and adoption keep manual ports and save only on submit @portpicker", async ({ app, page }) => {
	let gate = Promise.resolve();
	await stubScan(page, { poll: async () => { await gate; return { status: 200, json: completed() }; } });
	const writes = [];
	page.on("request", (request) => { if (["POST", "PATCH", "PUT"].includes(request.method()) && !request.url().includes("network-scans") && !request.url().includes("preview")) writes.push(request.url()); });
	const picker = await openWizard(page, app);
	await addManual(picker, "8080 udp:123");
	// Loading state while the job runs: busy button, cancel offered, form stays editable.
	let release;
	gate = new Promise((resolve) => { release = resolve; });
	await scan(picker).click();
	await picker.locator("[data-scan-spinner]").waitFor({ state: "visible" });
	assert.equal(await scan(picker).isDisabled(), true);
	assert.equal(await scan(picker).getAttribute("aria-busy"), "true");
	assert.equal(await picker.getByRole("button", { name: "Cancel scan" }).isVisible(), true);
	await page.locator("#wizard-name").fill("typed while scanning");
	assert.equal(await page.locator("#wizard-name").inputValue(), "typed while scanning", "the form is not blocked");
	release();
	await picker.locator("[data-scan-results]").waitFor({ state: "visible" });
	assert.equal(await scan(picker).isEnabled(), true);
	assert.match(await message(picker), /4 ports found: 3 confirmed open, 1 unclear\./);
	// No table: a compact confirmed list first, preselected, with the primary action showing the count.
	assert.equal(await picker.locator("table").count(), 0);
	assert.equal(await picker.locator("[data-scan-confirmed-title]").innerText(), "3 confirmed open ports");
	const ssh = await rowText(picker, "tcp", 22);
	assert.match(ssh, /TCP 22\s+ssh · OpenSSH · 9\.2\s+Open/);
	assert.deepEqual(await ticked(picker), ["tcp:22", "tcp:443", "udp:53"], "confirmed open ports are preselected, nothing else");
	assert.equal(await picker.locator("[data-scan-action=apply]").innerText(), "Add selected ports (3)");
	assert.equal(await picker.getByRole("button", { name: "Select all confirmed open ports" }).count(), 0, "the preselection replaces the bulk action");
	// The unclear port sits behind a collapsed disclosure and keeps its Nmap words; a table hint is no detection.
	const other = picker.locator("[data-scan-other]");
	assert.equal(await other.evaluate((node) => node.open), false);
	assert.equal(await other.locator("summary").innerText(), "1 other result – not confirmed open");
	await other.locator("summary").click();
	const snmp = await rowText(picker, "udp", 161);
	assert.match(snmp, /UDP 161\s+snmp \(table hint\)\s+Open or filtered/);
	assert.doesNotMatch(snmp.replace(/open or filtered|open\|filtered/gi, ""), /\bopen\b/i, "an unclear UDP state is never labelled open");
	assert.doesNotMatch(snmp, /uncertain/i, "the shared note replaces the per-row repetition");
	assert.match(await other.innerText(), /not confirmed open/);
	// Selection changes the count; unclear ports can be ticked by hand.
	await picker.getByRole("checkbox", { name: /UDP 161/ }).check();
	await picker.getByRole("checkbox", { name: /TCP 443/ }).uncheck();
	assert.equal(await picker.locator("[data-scan-action=apply]").innerText(), "Add selected ports (3)");
	// Adoption: protocols apart, no duplicates, manual entries kept, form updated at once, nothing saved yet.
	await addManual(picker, "22");
	await picker.getByRole("button", { name: "Add selected ports (3)" }).click();
	assert.equal(await page.locator("#wizard-ports").inputValue(), "22,8080");
	assert.equal(await page.locator("#wizard-udp-ports").inputValue(), "53,123,161");
	assert.equal(await page.locator('[name="network_scan_id"]').inputValue(), SCAN_ID);
	assert.match(await message(picker), /Added 0 TCP and 2 UDP ports/);
	assert.deepEqual(await chips(picker), ["TCP 22", "TCP 8080", "UDP 53", "UDP 123", "UDP 161"]);
	assert.deepEqual(writes, [], "adopting ports saves nothing");
	// The final submission carries the ports and the job id.
	await page.getByRole("button", { name: "Continue" }).click();
	await page.locator("#step-checks-title").waitFor({ state: "visible" });
	await page.getByRole("button", { name: "Continue" }).click();
	await page.locator("#step-recipients-title").waitFor({ state: "visible" });
	await page.getByRole("button", { name: "Continue" }).click();
	let body = null;
	await page.route("**/targets", async (route) => {
		if (route.request().method() !== "POST") return route.fallback();
		body = route.request().postDataJSON();
		return route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "stop" }) });
	});
	await page.getByRole("button", { name: "Start monitoring" }).click();
	await page.locator("#wizard-error:not(.d-none)").waitFor();
	assert.equal(body.tcp_ports, "22,8080");
	assert.equal(body.udp_ports, "53,123,161");
	assert.equal(body.network_scan_id, SCAN_ID);
	assert.equal(body.enable_tcp_connect, true);
	assert.equal(body.enable_udp_scan, true);
});
test("16 results with one open port: only that port is primary, 15 others are collapsed and unselected @portpicker", async ({ app, page }) => {
	await stubScan(page, { poll: async () => ({ status: 200, json: completed(SIXTEEN) }) });
	const picker = await openWizard(page, app);
	await runScan(picker);
	assert.match(await message(picker), /16 ports found: 1 confirmed open, 2 closed, 13 unclear\./);
	assert.equal(await picker.locator("[data-scan-confirmed-title]").innerText(), "1 confirmed open port");
	assert.deepEqual((await picker.locator("[data-scan-confirmed] li").allInnerTexts()).map((text) => text.replace(/\b(?:check_circle|cancel|help)\s*/g, "")), [await rowText(picker, "tcp", 80)]);
	assert.match(await rowText(picker, "tcp", 80), /TCP 80\s+http \(table hint\)\s+Open/);
	assert.equal(await picker.locator("[data-scan-confirmed] li").count(), 1);
	assert.deepEqual(await ticked(picker), ["tcp:80"]);
	assert.equal(await picker.locator("[data-scan-action=apply]").innerText(), "Add selected ports (1)");
	const other = picker.locator("[data-scan-other]");
	assert.equal(await other.evaluate((node) => node.open), false, "the other results start collapsed");
	assert.equal(await other.locator("summary").innerText(), "15 other results – not confirmed open");
	assert.equal(await picker.locator("[data-scan-other-list]").isVisible(), false, "collapsed rows are not rendered or focusable");
	await other.locator("summary").click();
	assert.equal(await picker.locator("[data-scan-other-list] li").count(), 15);
	assert.equal(await picker.locator("[data-scan-other-list] input:checked").count(), 0, "unclear and closed ports are never preselected");
	// Closed and unclear are told apart, in words, not only by colour.
	assert.match(await rowText(picker, "tcp", 21), /Closed/);
	assert.match(await rowText(picker, "tcp", 111), /Filtered/);
	assert.match(await rowText(picker, "udp", 161), /Open or filtered/);
	assert.equal(await picker.locator("[data-scan-other-list]").evaluate((node) => (node.textContent.match(/uncertain/gi) || []).length), 0, "no per-row repetition");
	// An unclear port can be chosen by hand and is adopted with the confirmed one.
	await picker.getByRole("checkbox", { name: /TCP 111 / }).check();
	assert.equal(await picker.locator("[data-scan-action=apply]").innerText(), "Add selected ports (2)");
	await picker.getByRole("button", { name: "Add selected ports (2)" }).click();
	assert.equal(await page.locator("#wizard-ports").inputValue(), "80,111");
	assert.equal(await page.locator("#wizard-udp-ports").inputValue(), "");
});
test("zero confirmed open ports gets a factual message and nothing preselected @portpicker", async ({ app, page }) => {
	await stubScan(page, { poll: async () => ({ status: 200, json: completed([{ protocol: "tcp", port: 22, state: "filtered" }, { protocol: "tcp", port: 23, state: "closed" }]) }) });
	const picker = await openWizard(page, app);
	await runScan(picker);
	assert.match(await message(picker), /2 ports found, none confirmed open \(1 closed, 1 unclear\)\./);
	assert.equal(await picker.locator("[data-scan-confirmed-title]").innerText(), "No confirmed open ports");
	const empty = await picker.locator("[data-scan-confirmed-empty]").innerText();
	assert.match(empty, /did not confirm any port as open/);
	assert.doesNotMatch(`${empty} ${await message(picker)}`, /all .*closed/i, "never claims every scanned port is closed");
	assert.equal(await picker.locator("[data-scan-confirmed] li").count(), 0);
	assert.equal(await ticked(picker).then((list) => list.length), 0);
	assert.equal(await picker.locator("[data-scan-action=apply]").isDisabled(), true);
	await openOthers(picker);
	await picker.getByRole("checkbox", { name: /TCP 22 / }).check();
	assert.equal(await picker.locator("[data-scan-action=apply]").isEnabled(), true);
	await picker.getByRole("button", { name: "Add selected ports (1)" }).click();
	assert.equal(await page.locator("#wizard-ports").inputValue(), "22");
});
test("error and empty states say what happened while manual entry keeps working @portpicker", async ({ app, page }) => {
	const { state, log } = await stubScan(page);
	const picker = await openWizard(page, app);
	await picker.locator('[data-scan-protocol="udp"]').check();
	const scenarios = [
		["timeout", { poll: failed("timeout") }, /^error: The scan timed out/],
		["permission", { poll: failed("permission_denied") }, /^error: Nmap lacks the permission.*raw-socket/],
		["process failure", { poll: failed("process_failed") }, /^error: The scan process failed/],
		["unreadable output", { poll: failed("invalid_xml") }, /^error: The scan result could not be read/],
		["cancelled elsewhere", { poll: job("cancelled") }, /^status: Scan cancelled/],
		["expired", { poll: job("expired") }, /^status: The scan result expired/],
		["no open ports", { poll: completed([]) }, /^status: No open ports found/],
		["unambiguous UDP missing", { poll: completed([PORTS[3]]) }, /^status: 1 port found, none confirmed open \(1 unclear\)\. No UDP port could be confirmed open/],
		["busy", { start: { status: 429, json: { detail: "Too many scans" } } }, /^error: A scan for this host is already running/],
		["invalid host", { start: { status: 422, json: { detail: [{ msg: "Invalid host" }] } } }, /^error: This host cannot be scanned/],
		["forbidden", { start: { status: 403, json: { detail: "Admin privileges required" } } }, /^error: Only administrators can scan/],
	];
	for (const [name, plan, expected] of scenarios) {
		state.start = async () => plan.start || { status: 202, json: job("queued", { result: null }) };
		state.poll = async () => ({ status: 200, json: plan.poll });
		await scan(picker).click();
		await page.waitForFunction(() => {
			const start = document.querySelector("[data-scan-action=start]");
			return !start.disabled && start.getAttribute("aria-busy") === null;
		});
		assert.match(await message(picker), expected, name);
		assert.equal(await scan(picker).isEnabled(), true, `${name}: the scan can be started again`);
		// Manual configuration works in every state.
		await addManual(picker, `${9000 + scenarios.findIndex((item) => item[0] === name)}`);
		assert.match(await message(picker), /Added 1 TCP and 0 UDP port/, name);
	}
	assert.equal(log.starts.length, scenarios.length);
	// Unclear-only: nothing is preselected, the apply action is disabled until the user ticks a port.
	state.start = async () => ({ status: 202, json: job("queued", { result: null }) });
	state.poll = async () => ({ status: 200, json: completed([PORTS[3]]) });
	await scan(picker).click();
	await picker.locator("[data-scan-results]").waitFor({ state: "visible" });
	assert.equal(await picker.locator("[data-scan-results] input:checked").count(), 0);
	assert.equal(await picker.locator("[data-scan-action=apply]").isDisabled(), true);
	await openOthers(picker);
	await picker.getByRole("checkbox", { name: /UDP 161/ }).check();
	await picker.getByRole("button", { name: "Add selected ports (1)" }).click();
	assert.match(await page.locator("#wizard-udp-ports").inputValue(), /(?:^|,)161(?:,|$)/);
	// A missing worker disables the scan with a visible reason; manual entry still works.
	state.start = async () => ({ status: 503, json: { detail: { code: "unavailable", message: "Nmap scan worker is restarting" } } });
	await scan(picker).click();
	await picker.locator("[data-scan-action=start]:disabled").waitFor();
	assert.match(await picker.locator("[data-scan-unavailable]").innerText(), /Nmap scan worker is restarting.*enter ports manually/is);
	await addManual(picker, "udp:5353");
	assert.match(await message(picker), /Added 0 TCP and 1 UDP port/);
});
test("a stale answer never replaces a newer scan or a changed host @portpicker", async ({ app, page }) => {
	let releaseFirst;
	const first = new Promise((resolve) => { releaseFirst = resolve; });
	let starts = 0;
	const { log } = await stubScan(page, {
		start: async () => ({ status: 202, json: job("queued", { id: ++starts === 1 ? SCAN_ID : OTHER_ID, result: null }) }),
		poll: async (id) => {
			if (id === SCAN_ID) { await first; return { status: 200, json: completed([{ protocol: "tcp", port: 1111, state: "open" }], SCAN_ID) }; }
			return { status: 200, json: completed([{ protocol: "tcp", port: 2222, state: "open" }], OTHER_ID) };
		},
	});
	const picker = await openWizard(page, app, "a.example.org");
	// Cancel the first scan, start a second; the late first answer must be ignored.
	await scan(picker).click();
	await picker.getByRole("button", { name: "Cancel scan" }).waitFor({ state: "visible" });
	await picker.getByRole("button", { name: "Cancel scan" }).click();
	await scan(picker).waitFor();
	await page.waitForFunction(() => !document.querySelector("[data-scan-action=start]").disabled);
	await scan(picker).click();
	await picker.locator("[data-scan-results]").waitFor({ state: "visible" });
	releaseFirst();
	await page.waitForTimeout(400);
	assert.deepEqual(await picker.locator("[data-scan-results] input").evaluateAll((nodes) => nodes.map((node) => node.dataset.port)), ["2222"]);
	assert.ok(log.deletes.includes(SCAN_ID), "the cancelled job was deleted");
	// Changing the host invalidates the result and the job id that would be sent with the form.
	assert.deepEqual(await ticked(picker), ["tcp:2222"], "the confirmed port is preselected");
	await picker.getByRole("button", { name: "Add selected ports (1)" }).click();
	assert.equal(await page.locator('[name="network_scan_id"]').inputValue(), OTHER_ID);
	await page.locator("#wizard-address").fill("b.example.org");
	await picker.locator("[data-scan-results]").waitFor({ state: "hidden" });
	assert.equal(await page.locator('[name="network_scan_id"]').inputValue(), "");
	assert.match(await message(picker), /Host changed/);
	assert.equal(await page.locator("#wizard-ports").inputValue(), "2222", "the ports already adopted stay in the form");
});
// Real visibility, not the attribute: a hidden result area has no box, and stale rows of an earlier scan or host are gone.
async function assertNoStaleResults(picker, phase) {
	const state = await picker.locator("[data-scan-results]").evaluate((node) => {
		const box = node.getBoundingClientRect();
		return { display: getComputedStyle(node).display, width: box.width, height: box.height, rows: node.querySelectorAll("input[type=checkbox]").length };
	});
	assert.equal(state.display, "none", `${phase}: results area is not displayed`);
	assert.deepEqual([state.width, state.height], [0, 0], `${phase}: results area has no box`);
	assert.equal(state.rows, 0, `${phase}: no stale result rows remain`);
	assert.equal(await picker.locator("[data-scan-action=apply]").isVisible(), false, `${phase}: no adoption action`);
}
// Scan, then walk through new scan, cancel and host change; hostInput is the host field of the surrounding form.
async function checkResultsLifecycle(page, picker, hostInput, gate) {
	await assertNoStaleResults(picker, "before the first scan");
	await runScan(picker);
	assert.equal(await picker.locator("[data-scan-results]").isVisible(), true);
	gate.close();
	await scan(picker).click();
	await picker.locator("[data-scan-spinner]").waitFor({ state: "visible" });
	await assertNoStaleResults(picker, "during a new scan");
	await picker.getByRole("button", { name: "Cancel scan" }).click();
	await assertNoStaleResults(picker, "after cancel");
	gate.open();
	await page.waitForFunction(() => !document.querySelector("[data-scan-action=start]").disabled);
	await runScan(picker);
	await hostInput.fill("other.example.org");
	await picker.locator("[data-scan-results]").waitFor({ state: "hidden" });
	await assertNoStaleResults(picker, "after a host change");
}
function pollGate() {
	let release = () => {};
	let pending = Promise.resolve();
	return {
		wait: () => pending,
		close: () => { pending = new Promise((resolve) => { release = resolve; }); },
		open: () => release(),
	};
}
test("results area is invisible and empty before a scan, during a new scan, after cancel and after a host change @portpicker", async ({ app, page }) => {
	const gate = pollGate();
	await stubScan(page, { poll: async () => { await gate.wait(); return { status: 200, json: completed() }; } });
	const picker = await openWizard(page, app);
	await checkResultsLifecycle(page, picker, page.locator("#wizard-address"), gate);
});
test("scanner text is rendered as text, never as HTML @portpicker", async ({ app, page }) => {
	await stubScan(page, {
		poll: async () => ({ status: 200, json: completed([
			{ protocol: "tcp", port: 80, state: "open", service: { name: "<img src=x onerror=\"window.__pwn=1\">", product: "<b>bold</b>", method: "probed" } },
			{ protocol: "tcp", port: 81, state: "<script>window.__pwn=2</script>", service: { name: "x", method: "table" } },
		]) }),
	});
	const picker = await openWizard(page, app);
	await runScan(picker);
	await openOthers(picker);
	assert.equal(await picker.locator("[data-scan-results] img, [data-scan-results] script, [data-scan-results] b").count(), 0);
	assert.match(await rowText(picker, "tcp", 80), /<img src=x onerror="window.__pwn=1"> · <b>bold<\/b>/);
	assert.equal(await page.evaluate(() => window.__pwn), undefined);
	// Error text from the server is text as well.
	const { state } = await stubScan(page);
	state.poll = async () => ({ status: 200, json: failed("custom", "<img src=x onerror=\"window.__pwn=3\">") });
	await scan(picker).click();
	await picker.locator("[data-scan-error]:not([hidden])").waitFor();
	assert.equal(await picker.locator("[data-scan-error] img").count(), 0);
	assert.match(await picker.locator("[data-scan-error]").innerText(), /<img src=x/);
	assert.equal(await page.evaluate(() => window.__pwn), undefined);
});
test("keyboard: Enter adds ports without submitting, results toggle with Space, disclosures open with Enter @portpicker", async ({ app, page }) => {
	await stubScan(page);
	const picker = await openWizard(page, app);
	const input = picker.locator("[data-manual-input]");
	await input.focus();
	await page.keyboard.type("udp:53 22");
	await page.keyboard.press("Enter");
	assert.deepEqual(await chips(picker), ["TCP 22", "UDP 53"]);
	assert.equal(await page.locator("#step-address-title").isVisible(), true, "Enter in the field does not advance or submit the wizard");
	assert.equal(await page.evaluate(() => document.activeElement?.hasAttribute("data-manual-input")), true, "focus stays in the field");
	// Tab reaches the chip remove buttons; Enter removes and focus moves to a control that still exists.
	await page.keyboard.press("Tab");
	await page.keyboard.press("Tab");
	assert.equal(await page.evaluate(() => document.activeElement?.getAttribute("aria-label")), "Remove TCP port 22");
	await page.keyboard.press("Enter");
	assert.deepEqual(await chips(picker), ["UDP 53"]);
	assert.equal(await page.evaluate(() => document.activeElement?.getAttribute("aria-label")), "Remove UDP port 53");
	// Result rows are real checkboxes.
	await runScan(picker);
	const first = picker.locator("[data-scan-confirmed] input").first();
	assert.equal(await first.isChecked(), true, "confirmed ports start selected");
	await first.focus();
	await page.keyboard.press("Space");
	assert.equal(await first.isChecked(), false);
	await page.keyboard.press("Space");
	assert.equal(await first.isChecked(), true);
	// The disclosure of the other results works from the keyboard.
	const summary = picker.locator("[data-scan-other] summary");
	await summary.focus();
	await page.keyboard.press("Enter");
	assert.equal(await picker.locator("[data-scan-other]").evaluate((node) => node.open), true);
	await page.keyboard.press("Enter");
	assert.equal(await picker.locator("[data-scan-other]").evaluate((node) => node.open), false);
});
// Vertical scroll containers inside the picker, apart from the wizard step that owns scrolling.
const nestedScrollers = () => {
	const root = document.querySelector("[data-network-scan-picker]");
	return [...root.querySelectorAll("*")]
		.filter((node) => node.offsetParent !== null)
		.filter((node) => ["auto", "scroll"].includes(getComputedStyle(node).overflowY) && node.scrollHeight > node.clientHeight + 1)
		.map((node) => node.className || node.tagName);
};
for (const viewport of [{ name: "1280x720", width: 1280, height: 720 }, { name: "390x844", width: 390, height: 844 }, { name: "320x640", width: 320, height: 640 }]) {
	for (const theme of ["light", "dark"]) {
		test(`picker with 16 results and many chips fits ${viewport.name} in ${theme} with one scroll region @portpicker`, async ({ app, page }) => {
			await page.setViewportSize({ width: viewport.width, height: viewport.height });
			await stubScan(page, { poll: async () => ({ status: 200, json: completed(SIXTEEN) }) });
			const picker = await openWizard(page, app);
			await page.evaluate((mode) => document.documentElement.setAttribute("data-bs-theme", mode), theme);
			await addManual(picker, Array.from({ length: 30 }, (_, index) => 1000 + index).join(" "));
			await runScan(picker);
			await openOthers(picker);
			const result = await page.evaluate(() => {
				const root = document.querySelector("[data-network-scan-picker]");
				const rect = root.getBoundingClientRect();
				const small = [...root.querySelectorAll("button:not([hidden]), input[type=checkbox], .tag-input__remove")]
					.filter((node) => node.offsetParent !== null)
					.map((node) => ({ node: node.closest("label") || node }))
					.map(({ node }) => ({ label: node.getAttribute("aria-label") || node.textContent.trim(), w: node.getBoundingClientRect().width, h: node.getBoundingClientRect().height }))
					.filter((box) => box.w < 24 || box.h < 24);
				const step = document.querySelector('.target-wizard-step:not([hidden])');
				const overflowing = [...root.querySelectorAll("*")].filter((node) => node.offsetParent !== null && node.getBoundingClientRect().right > window.innerWidth + 1).map((node) => node.className || node.tagName);
				return {
					pageOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
					inside: rect.left >= 0 && rect.right <= window.innerWidth + 1,
					small,
					overflowing,
					stepScrolls: step.scrollHeight > step.clientHeight,
				};
			});
			assert.equal(result.pageOverflow <= 0, true, `page overflows by ${result.pageOverflow}px`);
			assert.equal(result.inside, true);
			assert.deepEqual(result.overflowing, [], "nothing leaves the viewport horizontally");
			assert.deepEqual(result.small, [], "every control is at least 24 x 24 CSS px");
			assert.equal(result.stepScrolls, true, "the long expanded list is reached through the wizard step");
			assert.deepEqual(await page.evaluate(nestedScrollers), [], "the results add no second vertical scroll region");
			// Back and Continue stay reachable while the step scrolls.
			for (const name of ["Back", "Continue"]) {
				const box = await page.getByRole("button", { name }).boundingBox();
				assert.ok(box && box.y >= 0 && box.y + box.height <= viewport.height + 1, `${name} stays inside the viewport`);
			}
			// The last result is reachable by scrolling the step only.
			const last = picker.locator("[data-scan-other-list] li").last();
			await last.scrollIntoViewIfNeeded();
			const box = await last.boundingBox();
			const actions = await page.locator(".wizard-actions").boundingBox();
			assert.ok(box.y + box.height <= actions.y + 1, "the last result ends above the action bar");
		});
	}
}
// ----- target form (edit) -----
let editId;
test.describe("target form", () => {
	test.describe.configure({ mode: "serial" });
	test.beforeAll(async ({ browser, app, adminState }) => {
		const context = await browser.newContext({ storageState: adminState });
		const page = await context.newPage();
		await page.goto(`${app.base}/`, { waitUntil: "load" });
		editId = await page.evaluate(async () => {
			const body = { name: "Picker Edit", host: "edit.example.net", is_enabled: false, enable_ping: true, enable_tcp_connect: true, tcp_host: "edit.example.net", tcp_ports: "22", enable_udp_scan: true, udp_ports: "161" };
			const response = await window.apiFetch("/targets", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
			if (response.status !== 201) throw new Error(`HTTP ${response.status}`);
			return (await response.json()).id;
		});
		await context.close();
	});
	async function openEdit(page, app) {
		await page.goto(`${app.base}/ui/targets/${editId}/edit`);
		await page.locator("#target-network-tab").click();
		const picker = page.locator("[data-network-scan-picker]");
		await picker.waitFor();
		await openDiscover(picker);
		return picker;
	}
	test("edit: existing TCP and UDP ports are the starting point, scan adoption merges and the PATCH carries it @portpicker", async ({ app, page }) => {
		await stubScan(page);
		const picker = await openEdit(page, app);
		assert.deepEqual(await chips(picker), ["TCP 22", "UDP 161"]);
		assert.equal(await picker.locator('[data-scan-protocol="tcp"]').isChecked(), true);
		assert.equal(await picker.locator('[data-scan-protocol="udp"]').isChecked(), true);
		await runScan(picker);
		assert.deepEqual(await ticked(picker), ["tcp:22", "tcp:443", "udp:53"], "the editor preselects confirmed open ports as well");
		await picker.getByRole("button", { name: "Add selected ports (3)" }).click();
		assert.equal(await page.locator("#tcp_ports").inputValue(), "22,443");
		assert.equal(await page.locator("#udp_ports").inputValue(), "53,161");
		assert.deepEqual(await page.locator("#tcp-ports-tags .tcp-port-tag").evaluateAll((tags) => tags.map((tag) => tag.firstChild.textContent)), ["22", "443"], "the TCP tag input follows the adoption");
		let body = null;
		await page.route(`**/targets/${editId}`, async (route) => {
			if (route.request().method() === "GET") return route.fallback();
			body = route.request().postDataJSON();
			return route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "stop" }) });
		});
		await page.locator('#target-form button[type="submit"]').click();
		await page.waitForFunction(() => document.querySelector('#target-form button[type="submit"]') && !document.querySelector('#target-form button[type="submit"]').disabled);
		assert.equal(body.tcp_ports, "22,443");
		assert.equal(body.udp_ports, "53,161");
		assert.equal(body.network_scan_id, SCAN_ID);
		assert.equal(body.enable_ping, true, "ICMP is untouched");
	});
	test("edit: results area is invisible and empty before a scan, during a new scan, after cancel and after a host change @portpicker", async ({ app, page }) => {
		const gate = pollGate();
		await stubScan(page, { poll: async () => { await gate.wait(); return { status: 200, json: completed() }; } });
		const picker = await openEdit(page, app);
		const hostId = await picker.evaluate((root) => root.dataset.hostId);
		await checkResultsLifecycle(page, picker, page.locator(`#${hostId}`), gate);
	});
	test("edit: the editor uses the same picker component as the wizard @portpicker", async ({ app, page }) => {
		await stubScan(page);
		const hooks = (picker) => picker.evaluate((root) => [...new Set([root, ...root.querySelectorAll("*")].flatMap((node) => [...node.attributes].map((attr) => attr.name).filter((name) => name.startsWith("data-scan") || name.startsWith("data-manual") || name.startsWith("data-port"))))].sort());
		const editHooks = await hooks(await openEdit(page, app));
		const wizardHooks = await hooks(await openWizard(page, app));
		assert.deepEqual(editHooks, wizardHooks);
		assert.ok(editHooks.includes("data-manual-input") && editHooks.includes("data-scan-discover"));
	});
	test("edit: manual changes persist only with the form submission @portpicker", async ({ app, page }) => {
		await stubScan(page);
		const picker = await openEdit(page, app);
		await addManual(picker, "udp:53 443");
		await picker.getByRole("button", { name: "Remove UDP port 161" }).click();
		assert.deepEqual(await chips(picker), ["TCP 22", "TCP 443", "UDP 53"]);
		// Nothing is stored before the submission.
		const before = await page.evaluate(async (id) => (await (await window.apiFetch(`/targets/${id}`)).json()), editId);
		assert.equal(before.tcp_ports, "22");
		assert.equal(before.udp_ports, "161");
		await page.locator('#target-form button[type="submit"]').click();
		await page.waitForURL(new RegExp(`/ui/targets/${editId}$`), { timeout: 30000 });
		const after = await page.evaluate(async (id) => (await (await window.apiFetch(`/targets/${id}`)).json()), editId);
		assert.equal(after.tcp_ports, "22,443");
		assert.equal(after.udp_ports, "53");
		assert.deepEqual(after.network_ports.map((port) => `${port.protocol}:${port.port}`), ["tcp:22", "tcp:443", "udp:53"]);
		assert.equal(after.enable_ping, true);
	});
});
