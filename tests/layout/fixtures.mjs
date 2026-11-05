//
// tests/layout/fixtures.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import { test as base } from "@playwright/test";
import { seed, startApp } from "./harness.mjs";

// WCAG 2 contrast ratio of two computed colours ("rgb(...)"/"rgba(...)"); the alpha channel is ignored.
export function contrastRatio(a, b) {
	const luminance = (value) => {
		const match = /rgba?\(([^)]+)\)/.exec(value);
		if (!match) throw new Error(`unparsable colour: ${value}`);
		const [r, g, bl] = match[1].split(/[ ,/]+/).filter(Boolean).slice(0, 3).map((n) => {
			const c = Number(n) / 255;
			return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
		});
		return 0.2126 * r + 0.7152 * g + 0.0722 * bl;
	};
	const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
	return (hi + 0.05) / (lo + 0.05);
}

export const test = base.extend({
	app: [async ({}, use) => {
		const app = await startApp();
		try {
			await use(app);
		} finally {
			await app.stop();
		}
	}, { scope: "worker", timeout: 90_000 }],
	adminState: [async ({ app, browser }, use) => {
		await use(await seed(browser, app));
	}, { scope: "worker", timeout: 90_000 }],
});