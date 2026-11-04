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