//
// tests/layout/dashboard-widgets-modal.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// renderWidgetList() rebuilds the list after every Remove/Add; the pressed button must not take keyboard focus
// with it, otherwise Escape no longer reaches the modal (Bootstrap listens on the modal element).
import assert from "node:assert/strict";
import { test } from "./fixtures.mjs";
import { waitForVisualStability } from "./harness.mjs";

test("widgets modal keeps keyboard focus after Remove/Add and Escape still closes it @dashboard-widgets-modal", async ({ browser, app, adminState }) => {
	const context = await browser.newContext({ viewport: { width: 1280, height: 720 }, storageState: adminState });
	const page = await context.newPage();
	try {
		await page.goto(app.base + "/", { waitUntil: "load" });
		await page.waitForFunction(() => {
			const root = document.getElementById("dashboard-grid");
			return root?.gridstack && !root.classList.contains("dashboard-grid--loading") && !document.getElementById("dashboard-edit-toggle")?.disabled;
		}, null, { timeout: 15000 });
		await page.getByRole("button", { name: "Edit dashboard", exact: true }).click();
		await waitForVisualStability(page);
		await page.getByRole("button", { name: "Edit widgets", exact: true }).click();
		const modal = page.locator("#dashboard-widgets-modal");
		await modal.waitFor({ state: "visible" });
		await waitForVisualStability(page);

		const remove = modal.getByRole("button", { name: /^Remove widget: / }).first();
		const label = await remove.getAttribute("aria-label");
		const widgetName = label.replace(/^Remove widget: /, "");
		await remove.focus();
		await page.keyboard.press("Enter");

		const toggled = modal.getByRole("button", { name: `Add widget: ${widgetName}`, exact: true });
		await toggled.waitFor({ state: "visible" });
		assert.equal(await toggled.evaluate((el) => el === document.activeElement), true, "focus stays on the toggled widget button");

		await page.keyboard.press("Enter");
		const restored = modal.getByRole("button", { name: `Remove widget: ${widgetName}`, exact: true });
		await restored.waitFor({ state: "visible" });
		assert.equal(await restored.evaluate((el) => el === document.activeElement), true, "focus stays on the button after Add");

		await page.keyboard.press("Escape");
		await modal.waitFor({ state: "hidden" });
	} finally {
		await context.close();
	}
});
