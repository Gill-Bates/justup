//
// tests/js/downtime-time-zone.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Runs under a non-UTC zone so local rendering differs from UTC; CI runs in UTC otherwise.
process.env.TZ = "Europe/Berlin";

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const src = readFileSync(new URL("../../app/static/js/utils/downtime-time.js", import.meta.url), "utf8");
const ctx = vm.createContext({ Intl, Date });
vm.runInContext(src, ctx);
const range = vm.runInContext("formatDowntimeRange", ctx);
const NOW = new Date("2026-11-04T22:00:00Z");

test("local time moves a UTC range over midnight, UTC mode does not", () => {
	assert.equal(range("2026-11-04T22:30:00Z", "2026-11-04T23:10:00Z", { now: NOW }), "4 Nov 23:30 → 5 Nov 00:10");
	assert.equal(range("2026-11-04T22:30:00Z", "2026-11-04T23:10:00Z", { now: NOW, utc: true }), "4 Nov · 22:30–23:10");
});
