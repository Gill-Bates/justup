//
// tests/layout/test-support.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import { existsSync, readFileSync } from "node:fs";
import http from "node:http";
import path from "node:path";

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
		"utils/csrf.js", "utils/escape.js", "bootstrap.bundle.min.js", "utils/modal-focus.js", "utils/field-errors.js",
		"utils/optional-groups.js", "utils/confirm-dialog.js", ...(listFilter ? ["utils/list-filter.js"] : []),
		"app.js", "settings.recipients.js",
	];
}


// In-page reader for the state_badge macro (app/templates/fragments/_state_badge.html): the label is
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
