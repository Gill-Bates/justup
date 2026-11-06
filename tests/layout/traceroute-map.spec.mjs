//
// tests/layout/traceroute-map.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Traceroute map legibility (DESIGN.md, "Traceroute map"): the chain is the primary element, drawn
// between the markers with a halo; the basemap recedes; names come from the shared rules; the whole
// chain is in view. Fixture: Frankfurt, an estimated section across DE/NL/BE/FR, an outlier in Israel
// and the destination in Ratingen. Screenshots go to JUSTUP_TRACEROUTE_SHOTS when set.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test as base } from "@playwright/test";
import { waitForVisualStability } from "./harness.mjs";
import { resolvePythonExecutable, startStaticAppServer } from "./test-support.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const SHOTS = process.env.JUSTUP_TRACEROUTE_SHOTS;
const point = (hop, lat, lon, city, country, extra = {}) => ({ hop, lat, lon, city, country, ip: `192.0.2.${hop}`, ...extra });
const POINTS = [
	point(7, 50.11, 8.68, "Frankfurt", "DE", { map_role: "route", segment_to_next: "dashed", segment_note: "Hops 8 without a map position" }),
	point(9, 50.38, 7.33, "Kottenheim", "DE", { map_role: "estimate", segment_to_next: "dashed" }),
	point(10, 52.37, 4.9, "Amsterdam", "NL", { map_role: "estimate", segment_to_next: "dashed" }),
	point(11, 48.86, 2.35, "Paris", "FR", { map_role: "estimate", segment_to_next: "dashed" }),
	point(12, 32.08, 34.78, "Tel Aviv", "IL", { map_role: "estimate", segment_to_next: "dashed" }),
	point(13, 51.3, 6.85, "Ratingen", "DE", { map_role: "route", _destination: true }),
];
const html = () => {
	const fragment = execFileSync(resolvePythonExecutable(ROOT), [path.join(ROOT, "tests", "layout", "render_traceroute_fragment.py")], {
		input: JSON.stringify(POINTS), env: { ...process.env, PYTHONPATH: ROOT }, encoding: "utf8",
	});
	const css = ["bootstrap.min.css", "material-icons.css", "gentelella-layout.css", "ui.css", "traceroute-map.css"].map((f) => `<link rel="stylesheet" href="/static/css/${f}">`).join("");
	return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">${css}</head>
<body><main class="ui-page"><div class="traceroute-diagnostics">${fragment}</div></main><script src="/static/js/utils/escape.js"></script><script src="/static/js/traceroute-map.js"></script></body></html>`;
};
const test = base.extend({
	base: [async ({}, use) => {
		const page = html();
		const server = await startStaticAppServer({ root: ROOT, renderPage: () => page });
		try { await use(server.baseUrl); } finally { await server.close(); }
	}, { scope: "worker" }],
});
const VIEWPORTS = [{ name: "1280x720", width: 1280, height: 720 }, { name: "390x844", width: 390, height: 844 }];

const measure = (page) => page.evaluate(() => {
	const box = (el) => { const r = el.getBoundingClientRect(); return { x1: r.left, y1: r.top, x2: r.right, y2: r.bottom, cx: (r.left + r.right) / 2, cy: (r.top + r.bottom) / 2 }; };
	const root = document.getElementById("traceroute-map");
	const frame = box(root);
	const style = JSON.parse(root.dataset.mapStyle || "null");
	const paths = [...root.querySelectorAll(".leaflet-chainHalo-pane path, .leaflet-chain-pane path")]
		.filter((p) => p.getAttribute("stroke-width") !== null);
	const markers = [...root.querySelectorAll(".traceroute-hop-marker")].map(box);
	const labels = [...root.querySelectorAll(".tr-label")].map((el) => ({ name: el.textContent, ...box(el.querySelector(".tr-label-text") || el) }));
	const hit = (a, b) => a.x1 < b.x2 && a.x2 > b.x1 && a.y1 < b.y2 && a.y2 > b.y1;
	const dashed = paths.filter((p) => p.getAttribute("stroke-dasharray"));
	return {
		frame, style, markerCount: markers.length,
		markersInside: markers.every((m) => m.x1 >= frame.x1 - 1 && m.x2 <= frame.x2 + 1 && m.y1 >= frame.y1 - 1 && m.y2 <= frame.y2 + 1),
		widths: paths.map((p) => Number(p.getAttribute("stroke-width"))),
		dashes: dashed.map((p) => p.getAttribute("stroke-dasharray").replace(/,\s*/g, " ")),
		routeColors: [...new Set(paths.filter((p) => Number(p.getAttribute("stroke-width")) >= 3).map((p) => p.getAttribute("stroke").toLowerCase()))],
		labelNames: labels.map((l) => l.name),
		labelOverMarker: labels.some((l) => markers.some((m) => hit(l, m))),
		labelOverLabel: labels.some((a, i) => labels.some((b, j) => j > i && hit(a, b))),
		arrows: root.querySelectorAll(".traceroute-direction-arrow").length,
		legend: [...document.querySelectorAll(".tr-map-legend li")].map((li) => li.textContent.trim()),
		baseBorder: (() => { const p = root.querySelector(".leaflet-basemap-pane path"); return p ? { weight: Number(p.getAttribute("stroke-width")), color: p.getAttribute("stroke").toLowerCase() } : null; })(),
	};
});

for (const viewport of VIEWPORTS) {
	for (const theme of ["light", "dark"]) {
		test(`route map draws the chain, calm basemap and few names at ${viewport.name} ${theme} @traceroute-map`, async ({ page, base }) => {
			const errors = [];
			page.on("pageerror", (e) => errors.push(e.stack || String(e))); page.on("console", (m) => { if (m.type() === "error" || m.type() === "warning") errors.push(m.text()); });
			await page.setViewportSize({ width: viewport.width, height: viewport.height });
			await page.goto(base);
			await page.waitForSelector(".traceroute-hop-marker", { timeout: 20000 }).catch((e) => { throw new Error("no markers; page errors: " + errors.join(" | ")); });
			await page.waitForSelector(".leaflet-basemap-pane path", { state: "attached", timeout: 20000 }).catch(async () => { throw new Error("no basemap; panes: " + await page.evaluate(() => [...document.querySelectorAll(".leaflet-pane")].map((p) => p.className + ":" + p.querySelectorAll("path").length).join(" ; "))); });
			// The map listens for the application's theme event.
			await page.evaluate((mode) => { document.documentElement.setAttribute("data-bs-theme", mode); window.dispatchEvent(new Event("theme-changed")); }, theme);
			const border = JSON.parse(await page.getAttribute("#traceroute-map", "data-map-style")).palette[theme].border.toLowerCase();
			await page.waitForFunction((expected) => document.querySelector("#traceroute-map .leaflet-basemap-pane path")?.getAttribute("stroke")?.toLowerCase() === expected, border, { timeout: 10000 });
			await waitForVisualStability(page);
			if (SHOTS) await page.locator(".tr-map-frame").screenshot({ path: path.join(SHOTS, `map-${theme}-${viewport.name}.png`) });
			const m = await measure(page);
			const rules = m.style;
			assert.ok(rules, "the server hands the shared map style to the page (data-map-style)");
			const palette = rules.palette[theme];

			// The whole chain is in view, including the outlier.
			assert.equal(m.markerCount, POINTS.length, "one marker per chain node");
			assert.ok(m.markersInside, "every chain node is inside the map frame");

			// Chain: at least 3 px, in the shared route color, with a wider halo underneath, dashes from the rule.
			assert.ok(m.widths.some((w) => w >= rules.line.weight), `line weight ${m.widths}`);
			assert.ok(m.widths.some((w) => w >= rules.line.weight + rules.line.halo), "halo line is wider than the line");
			assert.ok(m.routeColors.includes(palette.route.toLowerCase()) && m.routeColors.includes(palette.halo.toLowerCase()), `colors ${m.routeColors}`);
			assert.ok(m.dashes.length > 0 && m.dashes.every((d) => d === rules.line.dash), `dashes ${m.dashes}`);

			// Quiet basemap.
			assert.ok(m.baseBorder && m.baseBorder.weight <= 0.5 && m.baseBorder.color === palette.border.toLowerCase(), JSON.stringify(m.baseBorder));

			// Names: only from the shared selection, at most the cap, never over a marker or each other.
			const selection = JSON.parse(await page.getAttribute("#traceroute-map", "data-map-labels"));
			const allowed = new Set([...selection.countries.map((c) => c.name), ...selection.cities.map((c) => c.name)]);
			assert.ok(m.labelNames.every((n) => allowed.has(n)), `unexpected names ${m.labelNames}`);
			// The cap is the server's own selection; the page must not add names beyond it.
			assert.ok(m.labelNames.length <= selection.countries.length + selection.cities.length, `too many names ${m.labelNames.length}`);
			assert.equal(m.labelOverMarker, false, "a name lies under a marker");
			assert.equal(m.labelOverLabel, false, "names overlap");

			// Estimated sections carry no direction arrow (every link of the fixture is dashed).
			assert.equal(m.arrows, 0, "arrows only on solid segments");

			// Legend names all node and link kinds.
			for (const text of ["Hop (reliable)", "Hop (approximate)", "Destination", "Connection measured", "Connection estimated or unknown"]) {
				assert.ok(m.legend.some((l) => l.includes(text)), `legend lacks "${text}": ${m.legend}`);
			}
			assert.deepEqual(errors, []);
		});
	}
}
