//
// tests/js/toast-tone.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// N05 / DC-STATE-3: an info toast is neutral (secondary), never the success colour.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
const SOURCE = readFileSync(new URL("../../app/static/js/app.js", import.meta.url), "utf8");
const WARNING_SOURCE = readFileSync(new URL("../../app/static/js/utils/warning.js", import.meta.url), "utf8");
function setup() {
	const classes = new Set();
	const toastEl = {
		classList: {
			add: (...n) => n.forEach((c) => classes.add(c)),
			remove: (...n) => n.forEach((c) => classes.delete(c)),
		},
	};
	const children = [];
	const bodyEl = { replaceChildren() { children.length = 0; }, appendChild: (c) => children.push(c) };
	const els = { "global-toast": toastEl, "global-toast-body": bodyEl };
	const ready = [];
	const element = () => ({ style: {}, className: "", textContent: "", setAttribute() {}, append() {} });
	const document = {
		cookie: "",
		documentElement: { getAttribute: () => null, setAttribute() {}, classList: { add() {}, remove() {} } },
		body: { classList: { add() {}, remove() {}, toggle() {} }, appendChild() {}, addEventListener() {}, removeEventListener() {} },
		readyState: "loading",
		addEventListener(type, fn) { if (type === "DOMContentLoaded") ready.push(fn); },
		getElementById: (id) => els[id] ?? null,
		createElement: element,
	};
	const sandbox = {
		document,
		bootstrap: { Toast: { getOrCreateInstance: () => ({ show() {} }) } },
		console,
		Headers,
	};
	sandbox.window = sandbox;
	vm.runInNewContext(WARNING_SOURCE, sandbox);
	// init() throws further down (theme, clock) on this minimal DOM; justUpToast is defined before that.
	try { vm.runInNewContext(SOURCE, sandbox); } catch { /* see above */ }
	for (const fn of ready) { try { fn(); } catch { /* unrelated init needs a full DOM */ } }
	return { toasts: sandbox.justUpToast, classes, children };
}
test("info toast uses the neutral secondary tone with the info icon, not success", () => {
	const { toasts, classes, children } = setup();
	toasts.info("fyi");
	assert.deepEqual([...classes], ["text-bg-secondary"]);
	assert.equal(children[0].textContent, "info");
});
test("success, error and warning toasts keep their own tones", () => {
	const { toasts, classes } = setup();
	toasts.success("ok");
	assert.deepEqual([...classes], ["text-bg-success"]);
	toasts.error("bad");
	assert.deepEqual([...classes], ["text-bg-danger"]);
	toasts.warning("hmm");
	assert.deepEqual([...classes], ["text-bg-warning"]);
});
