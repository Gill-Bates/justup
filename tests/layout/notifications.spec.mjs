//
// tests/layout/notifications.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { test as base } from "@playwright/test";
import { seed, startApp } from "./harness.mjs";
import {
	auditNotificationBehaviour,
	auditNotificationRows,
	NOTIFICATION_VIEWPORTS,
	seedNotifications,
} from "./notifications.mjs";

const test = base.extend({
	notificationApp: [async ({ browser }, use) => {
		const app = await startApp();
		try {
			const adminState = await seed(browser, app);
			seedNotifications(app);
			await use({ app, adminState });
		} finally {
			await app.stop();
		}
	}, { scope: "worker", timeout: 90_000 }],
	storageState: async ({ notificationApp }, use) => {
		await use(notificationApp.adminState);
	},
});

for (const viewport of NOTIFICATION_VIEWPORTS) {
	test.describe(`notification rows at ${viewport.name}`, () => {
		test.use({ viewport: { width: viewport.w, height: viewport.h }, deviceScaleFactor: viewport.dsf });
		test(`layout and interaction @notifications`, async ({ page, browserName, notificationApp }) => {
			const findings = await auditNotificationRows(page, notificationApp.app, browserName, viewport);
			assert.deepEqual(findings, [], `${browserName}/${viewport.name} notification findings`);
		});
	});
}

test("notification actions preserve links, read state and keyboard deletion @notifications", async ({ page, browserName, notificationApp }) => {
	await auditNotificationBehaviour(page, notificationApp.app, browserName);
});
