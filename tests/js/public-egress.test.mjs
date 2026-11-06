//
// tests/js/public-egress.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Display rules of the "Public outbound IPs" card: errors are never shown as addresses.
import assert from "node:assert/strict";
import test from "node:test";
import { describeFamily, describeLoading, describeRequestFailure, formatObserved, initPublicEgress } from "../../app/static/js/settings.public-egress.js";

test("an available address is copyable without success-detail text", () => {
	const view = describeFamily("IPv4", { status: "available", address: "93.184.216.34", sources: ["a", "b"] });
	assert.equal(view.address, "93.184.216.34");
	assert.equal(view.canCopy, true);
	assert.equal(view.detail, "");
});

test("unavailable, failed and inconsistent never carry an address or a copy action", () => {
	for (const status of ["unavailable", "failed", "inconsistent", "loading"]) {
		const view = describeFamily("IPv6", { status, address: "2001:4860:4860::8888", reason: "timeout" });
		assert.equal(view.address, "", status);
		assert.equal(view.canCopy, false, status);
	}
});

test("a stale value stays visible with its timestamp but is never copyable", () => {
	const view = describeFamily("IPv6", { status: "stale", address: "2a02:1::1", observed_at: "2026-11-05T19:00:00Z", reason: "no_route" });
	assert.equal(view.badgeText, "Stale");
	assert.equal(view.address, "2a02:1::1");
	assert.equal(view.canCopy, false);
	assert.match(view.detail, /Last confirmed .*2026.*not confirmed now/);
});

test("inconsistent results list the observed values as text", () => {
	const view = describeFamily("IPv4", { status: "inconsistent", observed: ["1.1.1.1", "8.8.8.8"], reason: "providers_disagree" });
	assert.match(view.detail, /1\.1\.1\.1, 8\.8\.8\.8/);
});

test("missing or malformed entries are a failed lookup, unknown status strings too", () => {
	for (const bad of [undefined, null, {}, { status: "<script>" }, "x"]) {
		const view = describeFamily("IPv6", bad);
		assert.equal(view.status, "failed");
		assert.equal(view.address, "");
		assert.equal(view.canCopy, false);
	}
});

test("formatObserved tolerates garbage", () => {
	assert.equal(formatObserved("nope"), "");
	assert.equal(formatObserved(null), "");
	assert.notEqual(formatObserved("2026-11-05T19:00:00Z"), "");
});

test("inherited object keys as status or reason are a failed lookup, not a TypeError", () => {
	for (const status of ["constructor", "toString", "__proto__", "hasOwnProperty", "valueOf"]) {
		const view = describeFamily("IPv4", { status, address: "93.184.216.34", reason: status });
		assert.equal(view.status, "failed", status);
		assert.equal(view.address, "", status);
		assert.equal(view.canCopy, false, status);
	}
	assert.equal(describeFamily("IPv4", { status: "failed", reason: "constructor" }).detail, "The lookup failed.");
});

test("available and stale need a non-empty string address, otherwise the entry is failed", () => {
	for (const status of ["available", "stale"]) {
		for (const address of [undefined, null, "", 42, {}, ["1.1.1.1"], true]) {
			const view = describeFamily("IPv4", { status, address });
			assert.equal(view.status, "failed", `${status} ${JSON.stringify(address)}`);
			assert.equal(view.address, "");
			assert.equal(view.canCopy, false);
		}
	}
});

test("loading keeps the last known address visible but not copyable", () => {
	const known = describeFamily("IPv4", { status: "available", address: "93.184.216.34", observed_at: "2026-11-05T19:00:00Z" });
	const loading = describeLoading(known);
	assert.equal(loading.status, "loading");
	assert.equal(loading.address, "93.184.216.34");
	assert.equal(loading.canCopy, false);
	assert.equal(loading.badgeText, "Checking\u2026");
	assert.equal(describeLoading(undefined).address, "");
});

test("a failed request marks the last known address stale with its timestamp, or reports a failed lookup", () => {
	const known = describeFamily("IPv6", { status: "available", address: "2a02:1::1", observed_at: "2026-11-05T19:00:00Z" });
	const stale = describeRequestFailure(known, "The server could not run the lookup (HTTP 500).");
	assert.equal(stale.status, "stale");
	assert.equal(stale.address, "2a02:1::1");
	assert.equal(stale.canCopy, false);
	assert.match(stale.detail, /Last confirmed .*HTTP 500/);
	const again = describeRequestFailure(stale, "x"); // a second failure keeps the original timestamp
	assert.equal(again.observedAt, "2026-11-05T19:00:00Z");
	const none = describeRequestFailure(undefined, "Too many requests.");
	assert.equal(none.status, "failed");
	assert.equal(none.address, "");
	assert.equal(none.detail, "Too many requests.");
});

// ─── DOM binding: re-init and HTMX re-render ─────────────────────────────────

class El {
	constructor(attrs = {}, children = {}) {
		this.attrs = { ...attrs };
		this.dataset = {};
		this.children = children;
		this.listeners = {};
		this.textContent = "";
		this.hidden = false;
		this.disabled = false;
		this.className = "";
		for (const [k, v] of Object.entries(attrs)) if (k.startsWith("data-")) this.dataset[k.slice(5).replace(/-(\w)/g, (_m, c) => c.toUpperCase())] = v;
		this.classList = { toggle() {} };
	}
	setAttribute(k, v) { this.attrs[k] = v; }
	getAttribute(k) { return this.attrs[k] ?? null; }
	removeAttribute(k) { delete this.attrs[k]; }
	addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
	querySelector(sel) { return this.children[sel] ?? null; }
	querySelectorAll(sel) { return this.children[sel] ?? []; }
}

function makeCard() {
	const row = (key) => {
		const parts = {
			"[data-egress-address]": new El(),
			"[data-egress-badge]": new El(),
			"[data-egress-detail]": new El(),
			"[data-egress-copy]": new El(),
		};
		const el = new El({ "data-egress-family": key }, parts);
		el.parts = parts;
		parts["[data-egress-copy]"].querySelector = () => null;
		return el;
	};
	const rows = [row("ipv4"), row("ipv6")];
	const card = new El({ "data-public-egress": "", "data-endpoint": "/x" }, {
		"[data-egress-family]": rows,
		"[data-egress-refresh]": new El(),
		"[data-egress-checked]": new El(),
		"[data-egress-cached]": new El(),
		"[data-egress-proxy]": new El(),
		"[data-egress-live]": new El(),
	});
	card.rows = rows;
	return card;
}

const ok = (body) => ({ ok: true, status: 200, json: async () => body });
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));
const v4 = (address, status = "available") => ({ ipv4: { status, address, observed_at: "2026-11-05T19:00:00Z", sources: ["a"] }, ipv6: { status: "unavailable" }, checked_at: "2026-11-05T19:00:00Z", cached: false, proxy_env: false });

test("initPublicEgress binds once per element and rebinds a re-rendered fragment", async () => {
	const calls = [];
	globalThis.window = { apiFetch: async (url) => { calls.push(url); return ok(v4("93.184.216.34")); } };
	const first = makeCard();
	const root = { current: first, querySelector() { return this.current; } };
	initPublicEgress(root);
	initPublicEgress(root); // repeated settle on the same element
	await flush();
	assert.deepEqual(calls, ["/x"]);
	assert.equal(first.listeners.click?.length, 1);
	assert.equal(first.querySelector("[data-egress-refresh]").listeners.click.length, 1);
	const second = makeCard(); // htmx swapped in a fresh fragment
	root.current = second;
	initPublicEgress(root);
	await flush();
	assert.deepEqual(calls, ["/x", "/x"]);
	assert.equal(second.querySelector("[data-egress-refresh]").listeners.click.length, 1);
	assert.equal(second.rows[0].parts["[data-egress-address]"].textContent, "93.184.216.34");
	delete globalThis.window;
});

test("a failing refresh keeps the last address as stale and not copyable; cached results are labelled", async () => {
	let mode = "ok";
	let cached = false;
	globalThis.window = {
		apiFetch: async () => {
			if (mode === "fail") return { ok: false, status: 500, json: async () => ({}) };
			return ok({ ...v4("93.184.216.34"), cached });
		},
	};
	const card = makeCard();
	initPublicEgress({ querySelector: () => card });
	await flush();
	const row = card.rows[0];
	const copy = row.parts["[data-egress-copy]"];
	assert.equal(copy.disabled, false);
	assert.equal(card.querySelector("[data-egress-cached]").hidden, true);
	cached = true;
	card.querySelector("[data-egress-refresh]").listeners.click[0]({ preventDefault() {} });
	await flush();
	assert.equal(card.querySelector("[data-egress-cached]").hidden, false);
	mode = "fail";
	card.querySelector("[data-egress-refresh]").listeners.click[0]({ preventDefault() {} });
	assert.equal(row.parts["[data-egress-address]"].textContent, "93.184.216.34"); // visible while checking
	assert.equal(copy.disabled, true);
	await flush();
	assert.equal(row.dataset.egressState, "stale");
	assert.equal(row.parts["[data-egress-address]"].textContent, "93.184.216.34");
	assert.equal(copy.disabled, true);
	assert.match(row.parts["[data-egress-detail]"].textContent, /Last confirmed.*HTTP 500/);
	delete globalThis.window;
});
