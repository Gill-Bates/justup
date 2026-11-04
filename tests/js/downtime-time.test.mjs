//
// tests/js/downtime-time.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const src = readFileSync(new URL("../../app/static/js/utils/downtime-time.js", import.meta.url), "utf8");
const ctx = vm.createContext({ Intl, Date });
vm.runInContext(src, ctx);
const range = vm.runInContext("formatDowntimeRange", ctx);
const precise = vm.runInContext("formatDowntimePrecise", ctx);
const NOW = new Date("2026-11-04T22:00:00Z");
const utc = { utc: true, now: NOW };

test("UTC ranges use the same compact rules as the server", () => {
	assert.equal(range("2026-11-04T18:57:06+00:00", "2026-11-04T21:50:06+00:00", utc), "4 Nov · 18:57–21:50");
	assert.equal(range("2026-11-04T23:50:00+00:00", "2026-11-05T00:15:00+00:00", utc), "4 Nov 23:50 → 5 Nov 00:15");
	assert.equal(range("2026-12-31T23:50:00+00:00", "2027-01-01T00:15:00+00:00", utc), "31 Dec 2026 23:50 → 1 Jan 2027 00:15");
	assert.equal(range("2025-03-02T10:00:00+00:00", "2025-03-02T10:05:00+00:00", utc), "2 Mar 2025 · 10:00–10:05");
	assert.equal(range("2026-11-04T13:06:00+00:00", "", utc), "4 Nov · since 13:06");
	assert.equal(range("2026-11-04T10:00:05+00:00", "2026-11-04T10:00:50+00:00", utc), "4 Nov · 10:00");
});

test("an unparseable start yields an empty string", () => {
	assert.equal(range("garbage", "", utc), "");
	assert.equal(precise("garbage", "", utc), "");
});

test("local time follows the zone and can move a range over midnight", () => {
	const zone = vm.runInContext("new Date('2026-11-04T22:30:00Z').getTimezoneOffset()", ctx);
	const local = range("2026-11-04T22:30:00Z", "2026-11-04T23:10:00Z", { now: NOW });
	if (zone === 0) assert.equal(local, "4 Nov · 22:30–23:10");
	else assert.match(local, /^\d+ [A-Z][a-z]{2}( · |\s)/);
});

test("precise text shows seconds and ongoing", () => {
	assert.equal(precise("2026-11-04T18:57:06+00:00", "2026-11-04T21:50:06+00:00", { utc: true }),
		"2026-11-04 18:57:06 UTC → 2026-11-04 21:50:06 UTC");
	assert.equal(precise("2026-11-04T13:06:00+00:00", "", { utc: true }), "2026-11-04 13:06:00 UTC → ongoing");
});
