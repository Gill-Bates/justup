//
// tests/layout/traceroute-map.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Traceroute map legibility: direction remains visible without reading hop numbers, dense regions
// get automatic details, and the basemap recedes. Screenshots go to JUSTUP_TRACEROUTE_SHOTS.
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
	point(1, 50.11, 8.68, "Frankfurt", "DE", { map_role: "route", _hop_range: "1-3", segment_to_next: "dashed", segment_note: "Hops 2–3 without a map position" }),
	point(4, 40.7128, -74.006, "New York", "US", { map_role: "route", segment_to_next: "solid" }),
	point(5, 49.8728, 8.6512, "Darmstadt", "DE", { map_role: "route", segment_to_next: "solid" }),
	point(6, 50.11, 8.68, "Frankfurt", "DE", { map_role: "route", segment_to_next: "solid" }),
	point(7, 50.12, 8.70, "Frankfurt edge", "DE", { map_role: "route", segment_to_next: "dashed" }),
	point(8, 40.73, -73.91, "New York", "US", { map_role: "estimate", segment_to_next: "dashed" }),
	point(9, 40.69, -74.10, "New York", "US", { map_role: "estimate", segment_to_next: "solid" }),
	point(10, 48.8566, 2.3522, "Paris", "FR", { map_role: "route", segment_to_next: "solid" }),
	point(11, 48.86, 2.36, "Paris edge", "FR", { map_role: "route", segment_to_next: "dashed" }),
	point(12, 52.52, 13.405, "Berlin", "DE", { map_role: "route", segment_to_next: "dashed", segment_note: "Hop 13 without a map position" }),
	point(14, 50.11, 8.68, "Frankfurt", "DE", { map_role: "route", segment_to_next: "solid" }),
	point(15, 49.8728, 8.6512, "Darmstadt", "DE", { map_role: "route", _destination: true }),
];
const html = (points = POINTS, totalHops = 15) => {
	const pointByHop = new Map(points.map((item) => [item.hop, item]));
	const hops = Array.from({ length: totalHops }, (_, index) => {
		const hop = index + 1;
		const mapped = pointByHop.get(hop);
		return { hop, ip: mapped?.ip || null, city: mapped?.city || null, country: mapped?.country || null,
			status: mapped ? "success" : "timeout", _destination: hop === totalHops };
	});
	const fragment = execFileSync(resolvePythonExecutable(ROOT), [path.join(ROOT, "tests", "layout", "render_traceroute_fragment.py")], {
		input: JSON.stringify({ points, hops, total_hops: totalHops }), env: { ...process.env, PYTHONPATH: ROOT }, encoding: "utf8",
	});
	const css = ["bootstrap.min.css", "material-icons.css", "gentelella-layout.css", "ui.css", "traceroute-map.css"].map((f) => `<link rel="stylesheet" href="/static/css/${f}">`).join("");
	return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">${css}</head>
<body><main class="ui-page"><div class="traceroute-diagnostics">${fragment}</div></main><script src="/static/js/utils/escape.js"></script><script src="/static/js/traceroute-map.js"></script></body></html>`;
};
const test = base.extend({
	base: [async ({}, use) => {
		let page = html();
		const server = await startStaticAppServer({ root: ROOT, renderPage: () => page });
		try { await use({ url: server.baseUrl, setPoints: (points, totalHops) => { page = html(points, totalHops); } }); }
		finally { await server.close(); }
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
	const routePaths = [...root.querySelectorAll(".leaflet-chain-pane .traceroute-route-segment")]
		.filter((path) => path.getAttribute("stroke-width") !== null && path.getAttribute("d"));
	const markers = [...root.querySelectorAll(".traceroute-hop-marker")].map((element) => ({ ...box(element), text: element.textContent }));
	const routePathsInside = routePaths.every((path) => {
		const rect = path.getBoundingClientRect();
		return rect.left >= frame.x1 - 1 && rect.right <= frame.x2 + 1 && rect.top >= frame.y1 - 1 && rect.bottom <= frame.y2 + 1;
	});
	const pathMarkerCollisions = routePaths.flatMap((path) => {
		const length = path.getTotalLength();
		const transform = path.getScreenCTM();
		for (let distance = 2; distance < length - 2; distance += 4) {
			const local = path.getPointAtLength(distance);
			const point = new DOMPoint(local.x, local.y).matrixTransform(transform);
			const marker = markers.find((rect) => point.x >= rect.x1 && point.x <= rect.x2 && point.y >= rect.y1 && point.y <= rect.y2);
			if (marker) return [{ segment: path.className.baseVal, point: [point.x, point.y], marker,
				offset: path.dataset.routeOffset, layoutHits: path.dataset.markerHits, pathData: path.getAttribute("d"),
				fromBox: path.dataset.routeFromBox, trimmedStart: path.dataset.routeTrimmedStart,
				startOffset: path.dataset.routeStartOffset, startTangent: path.dataset.routeStartTangent,
				markerSize: path.dataset.routeMarkerSize }];
		}
		return [];
	});
	const labels = [...root.querySelectorAll(".tr-label")].map((el) => ({ name: el.textContent, ...box(el.querySelector(".tr-label-text") || el) }));
	const hit = (a, b) => a.x1 < b.x2 && a.x2 > b.x1 && a.y1 < b.y2 && a.y2 > b.y1;
	const dashed = paths.filter((p) => p.getAttribute("stroke-dasharray"));
	return {
		frame, style,
		markerCount: markers.length,
		markersInside: markers.every((m) => m.x1 >= frame.x1 - 1 && m.x2 <= frame.x2 + 1 && m.y1 >= frame.y1 - 1 && m.y2 <= frame.y2 + 1),
		widths: paths.map((p) => Number(p.getAttribute("stroke-width"))),
		dashes: dashed.map((p) => p.getAttribute("stroke-dasharray").replace(/,\s*/g, " ")),
		routeColors: [...new Set(paths.filter((p) => Number(p.getAttribute("stroke-width")) >= 3).map((p) => p.getAttribute("stroke").toLowerCase()))],
		labelNames: labels.map((l) => l.name),
		labelOverMarker: labels.some((l) => markers.some((m) => hit(l, m))),
		labelOverLabel: labels.some((a, i) => labels.some((b, j) => j > i && hit(a, b))),
		arrows: [...root.querySelectorAll(".traceroute-direction-arrow")].map((arrow) => {
			const path = root.querySelector(`.leaflet-chain-pane .traceroute-route-segment--${arrow.dataset.segmentIndex}`);
			if (!path || !path.getTotalLength) return { index: Number(arrow.dataset.segmentIndex), tangentError: Infinity,
				found: Boolean(path), paths: [...root.querySelectorAll(".traceroute-route-segment")].map((node) => node.className.baseVal) };
			const length = path.getTotalLength();
			const transform = path.getScreenCTM();
			const arrowBox = arrow.getBoundingClientRect();
			const arrowCenter = { x: arrowBox.left + arrowBox.width / 2, y: arrowBox.top + arrowBox.height / 2 };
			let nearest = 0;
			let nearestDistance = Infinity;
			for (let distance = 0; distance <= length; distance += 2) {
				const rawPoint = path.getPointAtLength(distance);
				const point = new DOMPoint(rawPoint.x, rawPoint.y).matrixTransform(transform);
				const delta = Math.hypot(point.x - arrowCenter.x, point.y - arrowCenter.y);
				if (delta < nearestDistance) { nearestDistance = delta; nearest = distance; }
			}
			const beforeRaw = path.getPointAtLength(Math.max(0, nearest - 1));
			const afterRaw = path.getPointAtLength(Math.min(length, nearest + 1));
			const before = new DOMPoint(beforeRaw.x, beforeRaw.y).matrixTransform(transform);
			const after = new DOMPoint(afterRaw.x, afterRaw.y).matrixTransform(transform);
			const tangentAngle = Math.atan2(after.y - before.y, after.x - before.x) * 180 / Math.PI;
			const matrix = new DOMMatrixReadOnly(getComputedStyle(arrow).transform);
			const arrowAngle = Math.atan2(matrix.b, matrix.a) * 180 / Math.PI;
			let error = Math.abs(((arrowAngle - tangentAngle + 540) % 360) - 180);
			return { index: Number(arrow.dataset.segmentIndex), tangentError: error, offset: nearestDistance, length,
				tangentAngle, arrowAngle, arrowCenter, pathData: path.getAttribute("d") };
		}),
		routePathIndices: routePaths.map((path) => Number((path.getAttribute("class").match(/traceroute-route-segment--(\d+)/) || [])[1])),
		routePathsInside,
		pathMarkerCollisions,
		frameScrollWidth: root.closest(".tr-map-frame").scrollWidth,
		frameClientWidth: root.closest(".tr-map-frame").clientWidth,
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
			await page.goto(base.url);
			await page.waitForSelector(".traceroute-hop-marker", { timeout: 20000 }).catch((e) => { throw new Error("no markers; page errors: " + errors.join(" | ")); });
			await page.waitForSelector(".leaflet-basemap-pane path", { state: "attached", timeout: 20000 }).catch(async () => { throw new Error("no basemap; panes: " + await page.evaluate(() => [...document.querySelectorAll(".leaflet-pane")].map((p) => p.className + ":" + p.querySelectorAll("path").length).join(" ; "))); });
			// The map listens for the application's theme event.
			await page.evaluate((mode) => { document.documentElement.setAttribute("data-bs-theme", mode); window.dispatchEvent(new Event("theme-changed")); }, theme);
			const border = JSON.parse(await page.getAttribute("#traceroute-map", "data-map-style")).palette[theme].border.toLowerCase();
			await page.waitForFunction((expected) => document.querySelector("#traceroute-map .leaflet-basemap-pane path")?.getAttribute("stroke")?.toLowerCase() === expected, border, { timeout: 10000 });
			await waitForVisualStability(page);
			if (viewport.width === 390) {
				const status = await page.evaluate(() => {
					const scroll = document.querySelector(".tr-table-scroll");
					const cell = scroll.querySelector("tbody tr td:last-child");
					const visible = () => {
						const outer = scroll.getBoundingClientRect();
						const inner = cell.getBoundingClientRect();
						return inner.left >= outer.left - 1 && inner.right <= outer.right + 1;
					};
					const before = visible();
					scroll.scrollLeft = scroll.scrollWidth - scroll.clientWidth;
					const after = visible();
					scroll.scrollLeft = 0;
					return { scrollable: scroll.scrollWidth > scroll.clientWidth, before, after };
				});
				assert.ok(status.scrollable && status.before && status.after, `hop status must stay visible while scrolling: ${JSON.stringify(status)}`);
			}
			if (SHOTS) await page.locator(".tr-map-frame").screenshot({ path: path.join(SHOTS, `map-${theme}-${viewport.name}.png`) });
			const m = await measure(page);
			const rules = m.style;
			assert.ok(rules, "the server hands the shared map style to the page (data-map-style)");
			const palette = rules.palette[theme];

			// The whole chain is in view, including the outlier.
			assert.ok(m.markerCount > 0 && m.markerCount < POINTS.length, "dense overview groups stations and exposes their details");
			assert.ok(m.markersInside, "every chain node is inside the map frame");
			assert.ok(m.routePathsInside, "every overview route curve is inside the map frame");

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
			// Each selected name appears at most once.
			assert.equal(new Set(m.labelNames).size, m.labelNames.length, `duplicate names ${m.labelNames}`);
			assert.equal(m.labelOverMarker, false, "a name lies under a marker");
			assert.equal(m.labelOverLabel, false, "names overlap");

			// Arrows show hop order on solid and dashed paths, and follow each rendered curve.
			const arrowIndices = new Set(m.arrows.map((arrow) => arrow.index));
			for (const index of m.routePathIndices) assert.ok(arrowIndices.has(index),
				`visible segment ${index} has no direction arrow (${JSON.stringify({ arrowIndices: [...arrowIndices] })})`);
			assert.ok(m.arrows.length >= m.routePathIndices.length, "a visible arrow exists for every route curve");
			assert.ok(m.arrows.every((arrow) => arrow.tangentError < 12), `arrow is not tangent to its curve: ${JSON.stringify({ arrows: m.arrows, routePathIndices: m.routePathIndices })}`);
			assert.deepEqual(m.pathMarkerCollisions, [], `a route curve crosses an unrelated marker: ${JSON.stringify(m.pathMarkerCollisions)}`);
			assert.ok(m.frameScrollWidth <= m.frameClientWidth + 1, "the map frame does not widen the map panel");

			await page.locator(".tr-hop-row[data-hop-first='4']").click();
			assert.equal(await page.locator(".tr-step-btn[aria-pressed='true'][data-hop-first='4']").count(), 1, "table selection synchronizes the timeline");
			if (viewport.width === 1280 && theme === "light") {
				await page.locator(".leaflet-control-zoom-in").click();
				await page.setViewportSize({ width: 1160, height: viewport.height });
				await page.waitForFunction(() => {
					const map = document.getElementById("traceroute-map");
					return map?._leaflet_map && map._leaflet_map.getSize().x === map.clientWidth;
				});
				assert.equal(await page.locator(".tr-step-btn[aria-pressed='true'][data-hop-first='4']").count(), 1,
					"selection survives zoom and container resize");
			}
			await page.getByRole("button", { name: "Show entire route" }).click();
			assert.equal(await page.locator(".tr-step-btn[aria-pressed='true']").count(), 0, "reset clears selection across views");
			await page.locator(".tr-step-btn[data-hop-first='4']").click();
			assert.ok(await page.locator(".leaflet-chain-pane .traceroute-route-segment.is-selected").count() > 0,
				"timeline selection synchronizes the map");
			await page.getByRole("button", { name: "Show entire route" }).click();

			// Legend names all node and link kinds.
			for (const text of ["Hop (reliable)", "Hop (approximate)", "Destination", "Hop order between reliable positions", "Hop order; path estimated or unknown", "Arrows show hop order; curves are schematic"]) {
				assert.ok(m.legend.some((l) => l.includes(text)), `legend lacks "${text}": ${m.legend}`);
			}
			assert.deepEqual(errors, []);
		});
	}
}

test("the web map keeps date-line hops in the short world copy @traceroute-map", async ({ page, base }) => {
	base.setPoints([
		point(1, 10, 179, "West", "FJ", { map_role: "route", segment_to_next: "solid" }),
		point(2, 10, -179, "East", "US", { map_role: "route", _destination: true }),
	], 2);
	const errors = [];
	page.on("pageerror", (error) => errors.push(error.stack || String(error)));
	await page.setViewportSize({ width: 1280, height: 720 });
	await page.goto(base.url);
	await page.waitForSelector(".traceroute-hop-marker");
	const state = await page.evaluate(() => {
		const root = document.getElementById("traceroute-map");
		const map = root._leaflet_map;
		const bounds = map.getBounds();
		return {
			longitudeSpan: bounds.getEast() - bounds.getWest(),
			markerCount: root.querySelectorAll(".traceroute-hop-marker").length,
			labels: [...root.querySelectorAll(".traceroute-hop-marker")].map((marker) => marker.textContent),
		};
	});
	assert.ok(state.longitudeSpan < 20, `date-line route fit the long world copy: ${JSON.stringify(state)}`);
	assert.equal(state.markerCount, 1, `same-place endpoints remain a single overview marker: ${JSON.stringify(state)}`);
	assert.ok(state.labels[0].includes("S/D"));
	assert.deepEqual(errors, []);
});

test("30 hops in repeated dense regions consolidate into region markers @traceroute-map", async ({ page, base }) => {
	const points = Array.from({ length: 30 }, (_, index) => {
		const hop = index + 1;
		const localEurope = hop <= 10 || hop > 20;
		const place = localEurope ? "Frankfurt" : "New York";
		const lat = localEurope ? 50.11 + (hop % 3) * 0.01 : 40.71 + (hop % 3) * 0.01;
		const lon = localEurope ? 8.68 + (hop % 4) * 0.01 : -74 + (hop % 4) * 0.01;
		return point(hop, lat, lon, place, localEurope ? "DE" : "US", {
			map_role: "route",
			segment_to_next: hop % 5 === 0 ? "dashed" : "solid",
			...(hop === 30 ? { _destination: true } : {}),
		});
	});
	base.setPoints(points, 30);
	const errors = [];
	page.on("pageerror", (error) => errors.push(error.stack || String(error)));
	await page.setViewportSize({ width: 1280, height: 720 });
	await page.goto(base.url);
	await page.waitForSelector(".traceroute-hop-marker");
	const state = await page.evaluate(() => {
		const root = document.getElementById("traceroute-map");
		const markers = [...root.querySelectorAll(".traceroute-hop-marker")];
		return {
			markerCount: markers.length,
			titles: markers.map((marker) => marker.closest("[title]")?.getAttribute("title") || ""),
		};
	});
	assert.ok(state.markerCount <= 3, `dense regions did not consolidate in the overview: ${JSON.stringify(state)}`);
	assert.ok(state.titles.some((title) => title.includes("Frankfurt")), `no Frankfurt region marker: ${JSON.stringify(state)}`);
	assert.ok(state.titles.some((title) => title.includes("New York")), `no New York region marker: ${JSON.stringify(state)}`);
	assert.deepEqual(errors, []);
});
// OR-1 / DC-FOCUS-6: a keyboard-focused hop marker draws the shared V2 ring (neutral colour, shared width and offset) around the
// marker; the warning/link glow is a selection cue and no focus indicator.
for (const theme of ["light", "dark"]) {
	test(`a keyboard-focused hop marker draws the V2 ring (${theme}) @traceroute-map @focus`, async ({ page, base }) => {
		await page.setViewportSize({ width: 1280, height: 720 });
		await page.goto(base.url);
		await page.waitForSelector(".traceroute-hop-marker", { timeout: 20000 });
		await page.evaluate((mode) => { document.documentElement.setAttribute("data-bs-theme", mode); window.dispatchEvent(new Event("theme-changed")); }, theme);
		await waitForVisualStability(page);
		await page.keyboard.press("Tab"); // keyboard modality, so :focus-visible applies to the focus() call
		const icon = page.locator(".traceroute-hop-marker-icon").first();
		assert.equal(await icon.getAttribute("tabindex"), "0", "a hop marker is reachable by keyboard");
		await icon.focus();
		const state = await page.evaluate(() => {
			const probe = document.createElement("span");
			probe.style.cssText = "outline: var(--ju-focus-outline-width) solid var(--ju-focus-ring); outline-offset: var(--ju-focus-outline-offset); position: absolute; visibility: hidden;";
			document.body.append(probe);
			const want = getComputedStyle(probe);
			const tokens = { width: want.outlineWidth, colour: want.outlineColor, offset: want.outlineOffset };
			probe.remove();
			const focused = document.activeElement;
			const marker = focused.querySelector(".traceroute-hop-marker");
			const cs = getComputedStyle(marker);
			return { tokens, focusVisible: focused.matches(":focus-visible"), ring: { style: cs.outlineStyle, width: cs.outlineWidth, colour: cs.outlineColor, offset: cs.outlineOffset }, boxShadow: cs.boxShadow };
		});
		assert.equal(state.focusVisible, true, "the marker is focus-visible");
		assert.equal(state.ring.style, "solid");
		assert.deepEqual({ width: state.ring.width, colour: state.ring.colour, offset: state.ring.offset }, state.tokens, "the marker ring reads the shared focus tokens");
		assert.ok(parseFloat(state.tokens.offset) > 0, "the V2 offset is positive");
	});
}
