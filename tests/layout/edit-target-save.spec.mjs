//
// tests/layout/edit-target-save.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// Navigation contract of the target editor (DESIGN.md, "Target editor navigation"): a complete save of an
// existing target returns to the targets overview, every failed or partial save keeps the editor open,
// a save never runs twice, and the wizard still ends on the target detail page.
import assert from "node:assert/strict";
import { test as baseTest } from "./fixtures.mjs";
const test = baseTest.extend({
	storageState: async ({ adminState }, use) => {
		await use(adminState);
	},
});
test.setTimeout(90_000);
const VIEWPORTS = [{ name: "1280x720", width: 1280, height: 720 }, { name: "390x844", width: 390, height: 844 }];
// Longer than the editor's 800 ms redirect delay: a wrong navigation would have happened by then.
const PAST_REDIRECT_DELAY_MS = 1500;
const OVERVIEW = "/ui/targets";
async function createTarget(page, app, name) {
	await page.goto(`${app.base}${OVERVIEW}`);
	const id = await page.evaluate(async (targetName) => {
		const response = await window.apiFetch("/targets", {
			method: "POST", headers: { "Content-Type": "application/json" },
			body: JSON.stringify({ name: targetName, group: "Edit Save", host: "edit-save.example.org", enable_ping: true }),
		});
		return response.status === 201 ? (await response.json()).id : `HTTP ${response.status}`;
	}, name);
	assert.equal(typeof id, "number", `target fixture could not be created: ${id}`);
	return id;
}
async function deleteTarget(page, id) {
	await page.evaluate((targetId) => window.apiFetch(`/targets/${targetId}`, { method: "DELETE" }), id);
}
async function openEditor(page, app, id) {
	await page.goto(`${app.base}${OVERVIEW}/${id}/edit`);
	// The recipient list must be loaded, or the editor skips the assignment on purpose.
	await page.locator("#alert-users-checkboxes input").first().waitFor({ state: "attached" });
}
const saveButton = (page) => page.locator('#target-form button[type="submit"]');
const toastText = (page) => page.locator("#global-toast-body").innerText();
const pathOf = (page) => new URL(page.url()).pathname;
const isWrite = (request, suffix, method) => request.method() === method && new URL(request.url()).pathname.endsWith(suffix);
// Counts the writes of one editor session; the optional delay keeps a request in flight.
function countWrites(page, id, delayMs = 0) {
	const counts = { patch: 0, put: 0 };
	page.on("request", (request) => {
		if (isWrite(request, `/targets/${id}`, "PATCH")) counts.patch += 1;
		if (isWrite(request, `/targets/${id}/alert-users`, "PUT")) counts.put += 1;
	});
	if (delayMs) {
		page.route(`**/targets/${id}`, async (route) => {
			if (route.request().method() !== "PATCH") return route.fallback();
			await new Promise((resolve) => setTimeout(resolve, delayMs));
			return route.fallback();
		});
	}
	return counts;
}
for (const viewport of VIEWPORTS) {
	test.describe(`edit target save at ${viewport.name}`, () => {
		test.use({ viewport: { width: viewport.width, height: viewport.height } });
		test(`a complete save returns to the targets overview with the stored change @edit-target-save`, async ({ app, page, browserName }) => {
			const name = `P3 Save ${browserName} ${viewport.name}`;
			const renamed = `${name} renamed`;
			const id = await createTarget(page, app, name);
			try {
				await openEditor(page, app, id);
				await page.locator("#name-input, [name=name]").first().fill(renamed);
				const patched = page.waitForResponse((response) => isWrite(response.request(), `/targets/${id}`, "PATCH"));
				await saveButton(page).click();
				const response = await patched;
				assert.equal(response.status(), 200);
				assert.equal(response.request().postDataJSON().name, renamed);
				await page.waitForURL((url) => url.pathname === OVERVIEW);
				assert.equal(pathOf(page), OVERVIEW, "the editor must end on the overview, not on the target detail page");
				const stored = await page.evaluate(async (targetId) => (await (await window.apiFetch(`/targets/${targetId}`)).json()).name, id);
				assert.equal(stored, renamed);
				await page.locator("#targets-table").getByText(renamed).first().waitFor();
			} finally {
				await deleteTarget(page, id);
			}
		});
		for (const status of [422, 500]) {
			test(`a rejected save (${status}) keeps the editor open with its input @edit-target-save`, async ({ app, page, browserName }) => {
				const id = await createTarget(page, app, `P3 Fail${status} ${browserName} ${viewport.name}`);
				try {
					await openEditor(page, app, id);
					const detail = status === 422 ? [{ loc: ["body", "name"], msg: "Name is invalid" }] : "stop";
					await page.route(`**/targets/${id}`, (route) => route.request().method() === "PATCH"
						? route.fulfill({ status, contentType: "application/json", body: JSON.stringify({ detail }) })
						: route.fallback());
					const input = page.locator("#name-input, [name=name]").first();
					await input.fill("Kept input");
					await saveButton(page).click();
					await page.locator("#global-toast-body", { hasText: status === 422 ? "Name is invalid" : "stop" }).waitFor();
					await page.waitForTimeout(PAST_REDIRECT_DELAY_MS);
					assert.equal(pathOf(page), `${OVERVIEW}/${id}/edit`);
					assert.equal(await input.inputValue(), "Kept input");
					assert.equal(await saveButton(page).isEnabled(), true, "the user can correct the input and save again");
				} finally {
					await deleteTarget(page, id);
				}
			});
		}
		test(`a failed alert recipient save keeps the editor open and names the partial failure @edit-target-save`, async ({ app, page, browserName }) => {
			const id = await createTarget(page, app, `P3 Partial ${browserName} ${viewport.name}`);
			try {
				await openEditor(page, app, id);
				await page.route(`**/targets/${id}/alert-users`, (route) => route.request().method() === "PUT"
					? route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "boom" }) })
					: route.fallback());
				const patched = page.waitForResponse((response) => isWrite(response.request(), `/targets/${id}`, "PATCH"));
				await saveButton(page).click();
				assert.equal((await patched).status(), 200);
				await page.locator("#global-toast-body", { hasText: "Target saved, but alert recipients could not be saved" }).waitFor();
				await page.waitForTimeout(PAST_REDIRECT_DELAY_MS);
				assert.equal(pathOf(page), `${OVERVIEW}/${id}/edit`);
				assert.equal(await saveButton(page).isEnabled(), true);
			} finally {
				await deleteTarget(page, id);
			}
		});
		test(`a pending notification task is polled on the overview after the save @edit-target-save`, async ({ app, page, browserName }) => {
			const id = await createTarget(page, app, `P3 Notify ${browserName} ${viewport.name}`);
			try {
				await openEditor(page, app, id);
				await page.route(`**/targets/${id}/alert-users`, (route) => route.request().method() === "PUT"
					? route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ notification_task_id: "task-1", new_recipient_count: 1 }) })
					: route.fallback());
				await page.route(`**/targets/${id}/notification-status/task-1`, (route) => route.fulfill({
					status: 200, contentType: "application/json", body: JSON.stringify({ status: "completed", success: 1 }),
				}));
				const polled = page.waitForRequest((request) => new URL(request.url()).pathname === `/targets/${id}/notification-status/task-1`);
				await saveButton(page).click();
				await page.waitForURL((url) => url.pathname === OVERVIEW);
				await polled;
				await page.locator("#global-toast-body", { hasText: "1 recipient(s) notified successfully" }).waitFor();
				assert.equal(await page.evaluate(() => sessionStorage.getItem("pendingNotificationTask")), null);
			} finally {
				await deleteTarget(page, id);
			}
		});
		test(`repeated clicks and Enter never save twice, during the request or the redirect delay @edit-target-save`, async ({ app, page, browserName }) => {
			const id = await createTarget(page, app, `P3 Double ${browserName} ${viewport.name}`);
			try {
				await openEditor(page, app, id);
				const counts = countWrites(page, id, 600);
				const patched = page.waitForResponse((response) => isWrite(response.request(), `/targets/${id}`, "PATCH"));
				await saveButton(page).click();
				// While the PATCH is in flight: another click, Enter in a field and a scripted submit.
				await saveButton(page).click({ force: true }).catch(() => {});
				await page.locator("#name-input, [name=name]").first().press("Enter");
				await page.evaluate(() => document.getElementById("target-form").requestSubmit());
				await patched;
				// In the redirect delay after a complete save: the button has to stay locked.
				await page.locator("#global-toast-body", { hasText: "Target updated" }).waitFor();
				await page.evaluate(() => document.getElementById("target-form").requestSubmit());
				await page.locator("#name-input, [name=name]").first().press("Enter").catch(() => {});
				await page.waitForURL((url) => url.pathname === OVERVIEW);
				await page.waitForTimeout(500);
				assert.deepEqual(counts, { patch: 1, put: 1 });
			} finally {
				await deleteTarget(page, id);
			}
		});
	});
}
test("cancel in the editor returns to the targets overview @edit-target-save", async ({ app, page }) => {
	await page.goto(`${app.base}${OVERVIEW}/1/edit`);
	await page.getByRole("link", { name: "Cancel" }).click();
	await page.waitForURL((url) => url.pathname === OVERVIEW);
});
test("the wizard still ends on the new target's detail page @edit-target-save @wizard", async ({ app, page }) => {
	await page.goto(`${app.base}${OVERVIEW}/new`);
	await page.locator('input[name="profile"][value="custom"]').check({ force: true });
	await page.getByRole("button", { name: "Continue" }).click();
	await page.locator("#wizard-address").fill("wizard-detail.example.org");
	await page.getByRole("button", { name: "Continue" }).click();
	await page.locator("#step-checks-title").waitFor({ state: "visible" });
	await page.locator("#wizard-ping").setChecked(true, { force: true });
	await page.getByRole("button", { name: "Continue" }).click();
	await page.locator("#step-recipients-title").waitFor({ state: "visible" });
	await page.getByRole("button", { name: "Continue" }).click();
	const created = page.waitForResponse((response) => isWrite(response.request(), "/targets", "POST"));
	await page.getByRole("button", { name: "Start monitoring" }).click();
	const response = await created;
	assert.equal(response.status(), 201);
	const id = (await response.json()).id;
	try {
		await page.waitForURL((url) => url.pathname === `${OVERVIEW}/${id}`);
	} finally {
		await deleteTarget(page, id);
	}
});
