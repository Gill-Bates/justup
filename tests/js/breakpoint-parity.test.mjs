//
// tests/js/breakpoint-parity.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// DC-TOK-3: breakpoints that CSS and JavaScript both need must be equal.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
const read = (p) => readFileSync(new URL(`../../app/static/${p}`, import.meta.url), "utf8");
test("drawer breakpoint is the same in the shell CSS, sidebar.js and dashboard-grid.js", () => {
	const css = read("css/gentelella-layout.css");
	assert.match(css, /@media \(width <=991\.98px\)/);
	const query = /\(max-width: (\d+(?:\.\d+)?)px\)/;
	for (const file of ["js/sidebar.js", "js/dashboard-grid.js"]) {
		assert.equal(read(file).match(query)?.[1], "991.98", file);
	}
});
test("combined-chart fill mode query matches the target detail CSS (1200x800)", () => {
	const js = read("js/charts/chart-builder.js").match(/"\(min-width: (\d+)px\) and \(min-height: (\d+)px\)"/);
	assert.ok(js, "chart-builder.js has the fill-mode query");
	const css = read("css/target-detail.css").match(/@media \(width >=(\d+)px\) and \(height >=(\d+)px\)/);
	assert.ok(css, "target-detail.css has the fill-mode query");
	assert.deepEqual(js.slice(1), css.slice(1));
});
