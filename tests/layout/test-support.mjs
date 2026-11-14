//
// tests/layout/test-support.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import { execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import http from "node:http";
import path from "node:path";

// Four separate notions (DESIGN.md 1.2, DC-MEAS-1); a profile name must say which one it exercises:
//  - CSS viewport: window size in CSS px. Proves layout and media queries at that width (320 px = WCAG reflow).
//  - Effective browser zoom Z: a (W/Z)x(H/Z) CSS viewport at DPR Z. A viewport proxy; the browser zoom
//    feature itself is not driven, because Playwright has no zoom API.
//  - Text enlargement: root font size scaled at an unchanged CSS viewport (rem-based sizes grow, px sizes do not).
//  - Device pixel ratio (DPR): pixel density only; it proves no reflow and is never a zoom label.
export const cssViewport = (width, height) => ({
	name: `css-${width}x${height}`, width, height, w: width, h: height, dsf: 1, textScale: 1,
});
export const effectiveZoom = (width, height, zoom) => {
	const w = Math.round(width / zoom);
	const h = Math.round(height / zoom);
	return { name: `${width}x${height}-effective-zoom-${zoom * 100}pct`, width: w, height: h, w, h, dsf: zoom, textScale: 1 };
};
export const textEnlargement = (width, height, scale) => ({
	name: `css-${width}x${height}-text-${scale * 100}pct`, width, height, w: width, h: height, dsf: 1, textScale: scale,
});
// Scales the root font size after the page loaded; `!important` wins over the stylesheet's own root size.
export async function applyTextEnlargement(page, scale) {
	if (!scale || scale === 1) return;
	await page.addStyleTag({ content: `html:root { font-size: ${scale * 100}% !important; }` });
}
export function resolvePythonExecutable(repoRoot) {
	const virtualenvPython = path.join(repoRoot, ".venv", "bin", "python");
	return process.env.PYTHON || (existsSync(virtualenvPython) ? virtualenvPython : "python3");
}

export async function startStaticAppServer({ root, renderPage }) {
	const staticRoot = path.join(root, "app", "static");
	const server = http.createServer((request, response) => {
		const url = new URL(request.url, "http://localhost");
		if (url.pathname === "/") {
			response.setHeader("Content-Type", "text/html");
			response.end(renderPage());
			return;
		}
		if (!url.pathname.startsWith("/static/")) {
			response.statusCode = 404;
			response.end();
			return;
		}
		let file;
		try {
			file = path.resolve(staticRoot, decodeURIComponent(url.pathname.slice("/static/".length)));
		} catch {
			response.statusCode = 400;
			response.end();
			return;
		}
		if (!file.startsWith(`${staticRoot}${path.sep}`) || !existsSync(file)) {
			response.statusCode = 404;
			response.end();
			return;
		}
		const contentTypes = { ".css": "text/css", ".js": "text/javascript" };
		response.setHeader("Content-Type", contentTypes[path.extname(file)] || "application/octet-stream");
		response.end(readFileSync(file));
	});
	await new Promise((resolve, reject) => {
		server.once("error", reject);
		server.listen(0, "127.0.0.1", resolve);
	});
	return {
		baseUrl: `http://127.0.0.1:${server.address().port}/`,
		close: () => new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve())),
	};
}

// Waits until a chart canvas has left the "Loading data" state and owns a Chart instance.
// `withData` also needs a non-empty dataset; `allowNoData` accepts the "no data" label instead of a chart.
export function waitForChartReady(page, id = "chart-combined", { withData = false, allowNoData = false, timeout = 30000 } = {}) {
	return page.waitForFunction(({ canvasId, needData, noData }) => {
		const canvas = document.getElementById(canvasId);
		if (!canvas) return false;
		const label = canvas.getAttribute("aria-label") || "Loading data";
		if (/Loading data/i.test(label)) return false;
		const chart = window.Chart?.getChart(canvas);
		if (!chart) return noData && /no data/i.test(label);
		return !needData || chart.data.datasets.some((dataset) => dataset.data.length);
	}, { canvasId: id, needData: withData, noData: allowNoData }, { timeout });
}


// Scripts settings.recipients.js needs, in load order; `listFilter` adds the search helper some pages use.
export function recipientScripts({ listFilter = false } = {}) {
	return [
		"utils/csrf.js", "utils/escape.js", "utils/warning.js", "bootstrap.bundle.min.js", "utils/modal-focus.js", "utils/field-errors.js",
		"utils/optional-groups.js", "utils/confirm-dialog.js", "utils/status-badge.js", ...(listFilter ? ["utils/list-filter.js"] : []),
		"app.js", "settings.recipients.js",
	];
}


// Scripts the user dialogs need: the recipient scripts (channel switch behaviour) plus settings.users.js.
export function userChannelScripts() {
	return ["utils/api-error.js", ...recipientScripts(), "settings.users.js"];
}
// In-page reader for the state_dot macro (app/templates/fragments/_state_dot.html): the label is
// the text node next to the icon, the state is the visually hidden ": Up|Down|Unknown" suffix.
function readStateBadgesInPage(selector) {
	return [...document.querySelectorAll(selector)].map((badge) => ({
		label: [...badge.childNodes].filter((node) => node.nodeType === 3).map((node) => node.textContent).join("").trim(),
		state: badge.querySelector(".visually-hidden")?.textContent.replace(/^:\s*/, "") ?? "",
	}));
}

export const readStateBadges = (page, selector) => page.evaluate(`(${readStateBadgesInPage})(${JSON.stringify(selector)})`);

// Waits until more than `above` badges with the given label and state exist.
export function waitForStateBadgeCount(page, selector, { label, state, above = 0 }, timeout = 10000) {
	const test = `.filter((badge) => badge.label === ${JSON.stringify(label)} && badge.state === ${JSON.stringify(state)}).length > ${above}`;
	return page.waitForFunction(`(${readStateBadgesInPage})(${JSON.stringify(selector)})${test}`, null, { timeout });
}

export async function countStateBadges(page, selector, { label, state }) {
	return (await readStateBadges(page, selector)).filter((badge) => badge.label === label && badge.state === state).length;
}
// One Jinja environment for the static-fragment specs. `jobs` is a list of { template, context };
// the rendered fragments are concatenated in order.
const JINJA_RENDER = `
import json, sys
from jinja2 import Environment, FileSystemLoader, select_autoescape
env = Environment(loader=FileSystemLoader(sys.argv[1]), autoescape=select_autoescape(["html"]))
for job in json.loads(sys.argv[2]):
    print(env.get_template(job["template"]).render(**job.get("context", {})))
`;
export function renderTemplates(repoRoot, jobs) {
	return execFileSync(resolvePythonExecutable(repoRoot), ["-c", JINJA_RENDER, path.join(repoRoot, "app", "templates"), JSON.stringify(jobs)], { encoding: "utf8" });
}
