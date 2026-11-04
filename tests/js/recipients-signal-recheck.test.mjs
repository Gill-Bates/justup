//
// tests/js/recipients-signal-recheck.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Settings > Recipients "Re-check Signal": explicit action that bypasses the backend cache once.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const SOURCE = readFileSync(new URL("../../app/static/js/settings.recipients.js", import.meta.url), "utf8");
const PHONE = "+4915112345678";

function makeEl(extra = {}) {
	const attrs = new Map();
	const classes = new Set();
	return {
		textContent: "", title: "", dataset: {}, extra,
		classList: { add: (c) => classes.add(c), remove: (...c) => c.forEach((x) => classes.delete(x)), contains: (c) => classes.has(c) },
		setAttribute: (k, v) => attrs.set(k, String(v)),
		getAttribute: (k) => attrs.get(k) ?? null,
		removeAttribute: (k) => attrs.delete(k),
		attrs, classes, querySelector: () => null,
	};
}

function setup(respond) {
	const icon = makeEl();
	const statusIcon = makeEl();
	const status = makeEl();
	status.querySelector = () => statusIcon;
	const btnIcon = makeEl();
	btnIcon.textContent = "sync";
	const btn = makeEl();
	btn.dataset = { recipientId: "7", recipientName: "Tobias", recipientPhone: PHONE };
	btn.querySelector = () => btnIcon;
	const toasts = [];
	const requests = [];
	const sandbox = {
		console: { warn() {}, error() {}, log() {} },
		document: { addEventListener() {}, getElementById: (id) => (id === "signal-status-7" ? status : null) },
		justUpToast: { success: (m) => toasts.push(["success", m]), warning: (m) => toasts.push(["warning", m]), error: (m) => toasts.push(["error", m]) },
		escapeHtml: (v) => v,
		SettingsPage: {},
	};
	sandbox.window = sandbox;
	sandbox.apiFetch = async (url, options) => { requests.push([url, options]); return respond(); };
	vm.createContext(sandbox);
	vm.runInContext(SOURCE, sandbox);
	return { sandbox, btn, btnIcon, statusIcon, toasts, requests, icon };
}
const reply = (status, body) => () => ({ ok: status < 400, status, json: async () => body });

test("re-check posts to the recipient id, never the phone number, and shows the result as status", async () => {
	const t = setup(reply(200, { registered: false }));
	await t.sandbox.SettingsPage.Recipients.recheckSignalFromList(t.btn);
	assert.equal(t.requests.length, 1);
	assert.equal(t.requests[0][0], "/recipients/7/recheck-signal");
	assert.equal(t.requests[0][1].method, "POST");
	assert.ok(!JSON.stringify(t.requests).includes(PHONE.slice(1)), "no phone number in URL or body");
	assert.equal(t.statusIcon.textContent, "warning");
	assert.equal(t.statusIcon.getAttribute("aria-label"), "Not registered with Signal");
	assert.deepEqual(t.toasts, [["warning", "Tobias · Signal: Not registered with Signal"]]);
});

test("the button is busy while the request runs, keeps focusability and is restored afterwards", async () => {
	let release;
	const gate = new Promise((resolve) => { release = resolve; });
	const t = setup(() => gate.then(() => ({ ok: true, status: 200, json: async () => ({ registered: true }) })));
	const first = t.sandbox.SettingsPage.Recipients.recheckSignalFromList(t.btn);
	assert.equal(t.btn.getAttribute("aria-busy"), "true");
	assert.equal(t.btn.getAttribute("aria-disabled"), "true");
	assert.equal(t.btn.disabled, undefined, "not natively disabled: focus stays on the button");
	assert.equal(t.statusIcon.textContent, "hourglass_empty");
	await t.sandbox.SettingsPage.Recipients.recheckSignalFromList(t.btn); // double activation is ignored
	release();
	await first;
	assert.equal(t.requests.length, 1);
	assert.equal(t.btn.getAttribute("aria-busy"), null);
	assert.equal(t.btn.getAttribute("aria-disabled"), null);
	assert.equal(t.btnIcon.textContent, "sync");
	assert.equal(t.statusIcon.textContent, "check_circle");
});

test("a rejected request shows a compact failure state and a sanitized message", async () => {
	const t = setup(reply(429, { detail: "Signal check is busy. Try again shortly." }));
	await t.sandbox.SettingsPage.Recipients.recheckSignalFromList(t.btn);
	assert.equal(t.statusIcon.textContent, "help_outline");
	assert.equal(t.statusIcon.getAttribute("aria-label"), "Check failed");
	assert.equal(t.toasts[0][0], "warning");
	// The server detail needs the dynamic ES import, which a vm sandbox lacks: the fallback text applies.
	assert.equal(t.toasts[0][1], "Tobias · Signal: Signal check failed");
	assert.equal(t.btn.getAttribute("aria-busy"), null);
});

test("a network error never leaks its message and unknown payloads count as failure", async () => {
	const failing = setup(() => { throw new Error(`socket closed for ${PHONE}`); });
	await failing.sandbox.SettingsPage.Recipients.recheckSignalFromList(failing.btn);
	assert.deepEqual(failing.toasts, [["warning", "Tobias · Signal: Check failed"]]);
	assert.ok(!JSON.stringify(failing.toasts).includes(PHONE));
	const odd = setup(reply(200, { registered: "yes" }));
	await odd.sandbox.SettingsPage.Recipients.recheckSignalFromList(odd.btn);
	assert.equal(odd.statusIcon.getAttribute("aria-label"), "Check failed");
});
