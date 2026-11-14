//
// tests/js/public-egress.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Display rules of the "Public outbound IPs" card: errors are never shown as addresses.
import assert from "node:assert/strict";
import test from "node:test";
import { describeFamily, describeGeo, describeLoading, describeRequestFailure, formatObserved, initPublicEgress } from "../../app/static/js/settings.public-egress.js";

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

test("there is no stale state: an unknown status string is a failed lookup", () => {
	const view = describeFamily("IPv6", { status: "stale", address: "2a02:1::1", reason: "no_route" });
	assert.equal(view.status, "failed");
	assert.equal(view.address, "");
	assert.doesNotMatch(JSON.stringify(view), /[Ss]tale/);
});
test("every visible reason is one short line; the long text is kept for the title", () => {
	const reasons = ["no_route", "dns_failed", "timeout", "connect_failed", "tls_failed", "redirect", "http_status", "invalid_response", "response_too_large", "wrong_family", "non_public_address", "blocked_target", "providers_disagree", "busy"];
	for (const reason of reasons) {
		const view = describeFamily("IPv4", { status: "failed", reason });
		assert.ok(view.detail.length > 0 && view.detail.length <= 40, `${reason}: ${view.detail}`);
		assert.ok(view.title.length > view.detail.length - 1, reason);
	}
	assert.equal(describeFamily("IPv4", { status: "failed", reason: "blocked_target" }).detail, "Resolved to private address.");
});
test("geo line: flag, ASN and place, each part only when known; nothing known gives no line", () => {
	assert.deepEqual(describeGeo({ country_code: "de", country: "Germany", city: "Bonn", asn: "AS3320", as_org: "Deutsche Telekom AG" }),
		{ code: "DE", country: "Germany", label: "AS3320 Deutsche Telekom AG \u00b7 Bonn, Germany" });
	assert.deepEqual(describeGeo({ country_code: "US", country: "United States" }), { code: "US", country: "United States", label: "United States" });
	assert.deepEqual(describeGeo({ asn: "AS64500" }), { code: "", country: "", label: "AS64500" });
	assert.equal(describeGeo({ country_code: "FR", country: null }).label, "FR"); // unknown name falls back to the code
	for (const none of [undefined, null, {}, "x", { country_code: "", city: null, asn: null }, { country_code: "<script>", country: 5 }]) {
		assert.equal(describeGeo(none), null, JSON.stringify(none));
	}
	assert.equal(describeFamily("IPv4", { status: "available", address: "93.184.216.34", geo: { country_code: "US" } }).geo.code, "US");
	assert.equal(describeFamily("IPv4", { status: "failed", geo: { country_code: "US" } }).geo, null);
});
test("geo line without a local ASN database: flag and place only, no ASN text, no separator", () => {
	const absent = { country_code: "DE", country: "Germany", city: "Bonn", asn: null, as_org: null };
	assert.equal(describeGeo(absent).label, "Bonn, Germany");
	assert.equal(describeGeo({ ...absent, asn: "", as_org: "" }).label, "Bonn, Germany");
	assert.equal(describeGeo({ country_code: "DE", country: "Germany", asn: "AS3320", as_org: null }).label, "AS3320 \u00b7 Germany"); // number without operator
	assert.equal(describeGeo({ country_code: "DE", country: "Germany", city: "Berlin", asn: null, as_org: "Example AG" }).label, "Example AG \u00b7 Berlin, Germany");
	const long = describeGeo({ country_code: "DE", country: "Germany", asn: "AS1", as_org: "x".repeat(500) }); // untrusted length is bounded
	assert.ok(long.label.length < 160);
});

test("ASN and place survive a loading state and a failed request together with the address", () => {
	const geo = { country_code: "DE", country: "Germany", city: "Bonn", asn: "AS3320", as_org: "Deutsche Telekom AG" };
	const known = describeFamily("IPv4", { status: "available", address: "93.184.216.34", geo });
	assert.equal(describeLoading(known).geo.label, "AS3320 Deutsche Telekom AG \u00b7 Bonn, Germany");
	assert.equal(describeRequestFailure(known, "Server error (HTTP 500).").geo.label, "AS3320 Deutsche Telekom AG \u00b7 Bonn, Germany");
	// A server answer for the same address without geo (e.g. databases unreadable) is shown as it is: no invented line.
	assert.equal(describeFamily("IPv4", { status: "available", address: "93.184.216.34", geo: null }).geo, null);
});

test("inconsistent results list the observed values as text", () => {
	const view = describeFamily("IPv4", { status: "inconsistent", observed: ["1.1.1.1", "8.8.8.8"], reason: "providers_disagree" });
	assert.equal(view.detail, "Services disagree.");
	assert.match(view.title, /1\.1\.1\.1, 8\.8\.8\.8/);
	assert.equal(view.status, "inconsistent");
	assert.equal(view.address, "");
	assert.equal(view.canCopy, false);
	// Only strings are listed; a malformed entry never reaches the text.
	const mixed = describeFamily("IPv4", { status: "inconsistent", observed: ["1.1.1.1", 7, null, { a: 1 }, "8.8.8.8"], reason: "providers_disagree" });
	assert.match(mixed.title, /Observed: 1\.1\.1\.1, 8\.8\.8\.8\./);
	assert.doesNotMatch(mixed.title, /\[object|null|undefined|7/);
	// Without observed values no empty "Observed:" fragment is shown.
	const none = describeFamily("IPv4", { status: "inconsistent", observed: [], reason: "providers_disagree" });
	assert.doesNotMatch(none.title, /Observed/);
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
	assert.equal(describeFamily("IPv4", { status: "failed", reason: "constructor" }).detail, ""); // the badge already says it
});

test("available needs a non-empty string address, otherwise the entry is failed", () => {
	for (const status of ["available"]) {
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

test("a failed request keeps the last known address available and copyable, or reports a failed lookup", () => {
	const geo = { country_code: "DE", country: "Germany", city: "Bonn" };
	const known = describeFamily("IPv6", { status: "available", address: "2a02:1::1", geo });
	const kept = describeRequestFailure(known, "Server error (HTTP 500).");
	assert.equal(kept.status, "available");
	assert.equal(kept.badgeText, "Available");
	assert.equal(kept.address, "2a02:1::1");
	assert.equal(kept.canCopy, true);
	assert.equal(kept.detail, "");
	assert.equal(kept.geo.label, "Bonn, Germany");
	assert.equal(describeRequestFailure(kept, "x").address, "2a02:1::1"); // a second failure changes nothing
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
			"[data-egress-message]": new El(),
			"[data-egress-badge]": new El(),
			"[data-egress-dot]": new El(),
			"[data-egress-detail]": new El(),
			"[data-egress-copy]": new El(),
			"[data-egress-geo]": new El(),
		};
		parts["[data-egress-geo]"].replaceChildren = (...nodes) => { parts["[data-egress-geo]"].nodes = nodes; };
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

globalThis.document = {
	createElement: () => Object.assign(new El(), { append() {}, replaceChildren() {}, setAttribute() {}, addEventListener() {} }),
};
const ok = (body) => ({ ok: true, status: 200, json: async () => body });
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));
const v4 = (address, status = "available") => ({ ipv4: { status, address, observed_at: "2026-11-05T19:00:00Z", sources: ["a"] }, ipv6: { status: "unavailable" }, checked_at: "2026-11-05T19:00:00Z", cached: false, proxy_env: false });

test("initPublicEgress binds once per element and rebinds a re-rendered fragment", async () => {
	const calls = [];
	globalThis.window = {
		apiFetch: async (url) => { calls.push(url); return ok(v4("93.184.216.34")); },
		JuStatusDot: { classes: (tone) => `dot-${tone}` },
	};
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
	assert.equal(second.rows[0].parts["[data-egress-dot]"].className, "dot-success");
	assert.equal(second.rows[1].parts["[data-egress-dot]"].className, "dot-neutral");
	delete globalThis.window;
});

test("a failing refresh keeps the cached address and its geo line available; cached results are labelled", async () => {
	let mode = "ok";
	let cached = false;
	const geo = { country_code: "DE", country: "Germany", city: "Bonn", asn: "AS3320", as_org: "Deutsche Telekom AG" };
	globalThis.window = {
		JuStatusDot: { classes: (tone) => `dot-${tone}` },
		apiFetch: async () => {
			if (mode === "fail") return { ok: false, status: 500, json: async () => ({}) };
			const body = v4("93.184.216.34");
			return ok({ ...body, ipv4: { ...body.ipv4, geo }, cached });
		},
	};
	const card = makeCard();
	initPublicEgress({ querySelector: () => card });
	await flush();
	const row = card.rows[0];
	const copy = row.parts["[data-egress-copy]"];
	assert.equal(copy.disabled, false);
	assert.equal(row.parts["[data-egress-dot]"].className, "dot-success");
	assert.equal(card.querySelector("[data-egress-cached]").hidden, true);
	cached = true;
	card.querySelector("[data-egress-refresh]").listeners.click[0]({ preventDefault() {} });
	await flush();
	assert.equal(card.querySelector("[data-egress-cached]").hidden, false);
	mode = "fail";
	card.querySelector("[data-egress-refresh]").listeners.click[0]({ preventDefault() {} });
	assert.equal(row.parts["[data-egress-address]"].textContent, "93.184.216.34"); // visible while checking
	assert.equal(copy.disabled, true);
	assert.equal(row.parts["[data-egress-dot]"].className, "dot-neutral");
	await flush();
	assert.equal(row.dataset.egressState, "available");
	assert.equal(row.parts["[data-egress-badge]"].textContent, "Available");
	assert.equal(row.parts["[data-egress-address]"].textContent, "93.184.216.34");
	assert.equal(copy.disabled, false);
	assert.equal(row.parts["[data-egress-dot]"].className, "dot-success");
	assert.equal(row.parts["[data-egress-detail]"].textContent, "");
	assert.equal(row.parts["[data-egress-message]"].hidden, true);
	assert.equal(row.parts["[data-egress-geo]"].hidden, false); // the geo line survives a failing refresh
	assert.equal(row.parts["[data-egress-geo]"].title, "AS3320 Deutsche Telekom AG \u00b7 Bonn, Germany");
	delete globalThis.window;
});
test("an unreachable app shows only the Lookup failed badge, without a detail line", async () => {
	globalThis.window = {
		JuStatusDot: { classes: (tone) => `dot-${tone}` },
		apiFetch: async () => { throw new TypeError("network"); },
	};
	const card = makeCard();
	initPublicEgress({ querySelector: () => card });
	await flush();
	for (const row of card.rows) {
		assert.equal(row.dataset.egressState, "failed");
		assert.equal(row.parts["[data-egress-badge]"].textContent, "Lookup failed");
		assert.equal(row.parts["[data-egress-detail]"].textContent, "");
		assert.equal(row.parts["[data-egress-message]"].hidden, true);
	}
	delete globalThis.window;
});
test("the geo line sits under a found address, survives a refresh and is hidden without data", async () => {
	let body = { ...v4("93.184.216.34"), ipv4: { ...v4("93.184.216.34").ipv4, geo: { country_code: "DE", country: "Germany", city: "Bonn", asn: "AS3320", as_org: "Deutsche Telekom AG" } } };
	globalThis.window = {
		JuStatusDot: { classes: (tone) => `dot-${tone}` },
		apiFetch: async () => ok(body),
	};
	const card = makeCard();
	initPublicEgress({ querySelector: () => card });
	await flush();
	const geo = card.rows[0].parts["[data-egress-geo]"];
	assert.equal(geo.hidden, false);
	assert.equal(geo.title, "AS3320 Deutsche Telekom AG \u00b7 Bonn, Germany");
	assert.equal(card.rows[0].parts["[data-egress-copy]"].dataset.egressValue, "93.184.216.34"); // copy stays IP only
	assert.equal(card.rows[1].parts["[data-egress-geo]"].hidden, true); // unavailable row: no line
	body = v4("93.184.216.34"); // address without any geo data
	card.querySelector("[data-egress-refresh]").listeners.click[0]({ preventDefault() {} });
	await flush();
	assert.equal(geo.hidden, true);
	delete globalThis.window;
});
