//
// tests/js/dashboard-uptime-format.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// The server never shows "100%" for a series with a failure (app/utils/formatters.py format_availability).
// The JS-rendered uptime tile of the dashboard has to follow the same rule.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
const src = readFileSync(new URL("../../app/static/js/dashboard-monitoring.js", import.meta.url), "utf8");
function element(tag) {
	const el = {
		tag, textContent: "", className: "", title: "", children: [], dataset: {}, attributes: {},
		classList: { contains: () => false },
		append(...nodes) { this.children.push(...nodes); },
		replaceChildren(...nodes) { this.children = nodes; },
		setAttribute(name, value) { this.attributes[name] = String(value); },
		removeAttribute() {},
		querySelector: () => null,
		querySelectorAll: () => [],
	};
	return el;
}
// Renders the "uptime-day" tile for one server answer and returns the text of its big number.
async function renderUptime(payload) {
	const body = element("div");
	const tile = element("div");
	tile.dataset.widgetId = "uptime-day";
	tile.querySelector = (selector) => (selector === ".dashboard-monitor-body" ? body : null);
	const root = element("div");
	root.querySelectorAll = () => [tile];
	const listeners = [];
	const context = {
		AbortController, AbortSignal, Promise, Number, Map, Date, console,
		setTimeout: (fn) => (listeners.push(fn), 0), clearTimeout() {},
		parseUtcTimestamp: Date.parse,
		window: {
			apiFetch: async () => ({ ok: true, json: async () => payload }),
			addEventListener() {}, removeEventListener() {},
		},
		document: {
			visibilityState: "visible",
			getElementById: (id) => (id === "dashboard-grid" ? root : null),
			createElement: element,
			addEventListener() {}, removeEventListener() {},
			body: { addEventListener() {}, removeEventListener() {} },
		},
	};
	vm.runInNewContext(src, context);
	for (let i = 0; i < 10; i += 1) await new Promise((resolve) => setImmediate(resolve));
	const big = body.children.find((child) => child.className === "dashboard-monitor-number");
	return big ? big.textContent : null;
}
test("an uptime below 100 % is never rendered as 100 %", async () => {
	for (const value of [99.95, 99.96, 99.996, 99.9999]) {
		const text = await renderUptime({ uptime_percent: value, coverage_percent: 100 });
		assert.ok(text, `no number rendered for ${value}`);
		assert.notEqual(parseFloat(text), 100, `${value} rendered as ${text}`);
		// Two decimals show 99.95 and 99.96 exactly; only what would round up to 100.00 takes the server ceiling.
		if (value >= 99.995) assert.equal(text, "99.99%");
	}
});
test("an exact 100 % stays 100 % and ordinary values keep their rounding", async () => {
	assert.match(await renderUptime({ uptime_percent: 100, coverage_percent: 100 }), /^100(\.00)?%$/);
	assert.equal(await renderUptime({ uptime_percent: 97.123, coverage_percent: 100 }), "97.12%");
});
