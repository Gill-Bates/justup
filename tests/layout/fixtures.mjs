//
// tests/layout/fixtures.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import { test as base } from "@playwright/test";
import { seed, startApp } from "./harness.mjs";

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