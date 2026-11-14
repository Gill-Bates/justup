//
// tests/layout/user-channels.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Create and Edit User dialogs: notification channels of the user's recipient. Channel fields follow their switch
// (DESIGN.md "Optional field groups"), "Recipient Enabled" needs a channel, and user and recipient are saved in one
// request. The API is replaced by an in-page stub; the recipient list proves that the dialogs show the same record.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test as base } from "@playwright/test";
import { startStaticAppServer, userChannelScripts } from "./test-support.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const MODALS = path.join(ROOT, "app", "templates", "settings", "modals.html");
const PAGE = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/static/css/bootstrap.min.css"><link rel="stylesheet" href="/static/css/material-icons.css"><link rel="stylesheet" href="/static/css/gentelella-layout.css"><link rel="stylesheet" href="/static/css/ui.css">
</head><body><button type="button" id="opener">Open</button><div id="users-list"></div><div id="recipients-list"></div>
${readFileSync(MODALS, "utf8").replace(/\{#[\s\S]*?#\}/g, "")}
${userChannelScripts().map((src) => `<script src="/static/js/${src}"></script>`).join("")}</body></html>`;

const test = base.extend({
	base: [async ({}, use) => {
		const server = await startStaticAppServer({ root: ROOT, renderPage: () => PAGE });
		try {
			await use(server.baseUrl);
		} finally {
			await server.close();
		}
	}, { scope: "worker" }],
});

const PHONE = "+4915112345678";

async function transitionModal(page, modalId, action) {
	await page.evaluate(async ({ id, act }) => {
		const modal = document.getElementById(id);
		const settled = new Promise((resolve) => modal.addEventListener(act === "show" ? "shown.bs.modal" : "hidden.bs.modal", resolve, { once: true }));
		window.bootstrap.Modal.getOrCreateInstance(modal)[act]();
		await settled;
	}, { id: modalId, act: action });
}

// In-page API stub: a tiny in-memory server for /users and /recipients. Requests are recorded in window.__requests.
async function installApi(page, { users, recipients = [], failWith = null }) {
	await page.evaluate(({ initialUsers, initialRecipients, fail }) => {
		window.__requests = [];
		window.__confirms = [];
		window.__users = structuredClone(initialUsers);
		window.__recipients = structuredClone(initialRecipients);
		window.confirmAction = (options) => { window.__confirms.push(options.title); return Promise.resolve(true); };
		const respond = (body, status = 200) => Promise.resolve({
			ok: status < 400, status, headers: { get: () => "application/json" },
			json: () => Promise.resolve(body), text: () => Promise.resolve(JSON.stringify(body)), clone() { return this; },
		});
		window.apiFetch = (url, options = {}) => {
			const method = options.method || "GET";
			const body = options.body ? JSON.parse(options.body) : null;
			window.__requests.push({ url, method, body });
			if (fail && method !== "GET") return respond({ detail: fail.detail }, fail.status);
			if (url === "/users" && method === "GET") return respond(window.__users);
			if (url === "/recipients" && method === "GET") return respond(window.__recipients);
			if (url === "/users" && method === "POST") {
				const id = window.__users.length + 1;
				const recipient = { id: 100 + id, phone: body.recipient.phone, email: body.recipient.email, is_enabled: body.recipient.is_enabled, enable_for_all_targets: body.recipient.enable_for_all_targets };
				window.__users.push({ id, username: body.username, is_admin: body.is_admin, can_close_alerts: body.can_close_alerts, is_active: true, recipient });
				return respond(window.__users.at(-1));
			}
			const put = url.match(/^\/users\/(\d+)$/);
			if (put && method === "PUT") {
				const user = window.__users.find((u) => String(u.id) === put[1]);
				Object.assign(user, { username: body.username, is_admin: body.is_admin });
				Object.assign(user.recipient, body.recipient);
				return respond(user);
			}
			return respond([]);
		};
	}, { initialUsers: users, initialRecipients: recipients, fail: failWith });
}

const visible = (page, id) => page.evaluate((i) => {
	const el = document.getElementById(i);
	return !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== "hidden";
}, id);
const posts = (page, method) => page.evaluate((m) => window.__requests.filter((r) => r.method === m), method);

const USER = { id: 7, username: "alice", is_admin: false, can_close_alerts: false, is_active: true, recipient: { id: 41, phone: PHONE, email: null, is_enabled: true, enable_for_all_targets: true } };
const BARE = { id: 8, username: "bob", is_admin: false, can_close_alerts: false, is_active: true, recipient: { id: 42, phone: null, email: null, is_enabled: false, enable_for_all_targets: false } };

for (const viewport of [{ width: 1280, height: 720 }, { width: 390, height: 844 }]) {
	test(`Create User saves the channels together with the user at ${viewport.width}px @modal @user-channels`, async ({ page, base }) => {
		await page.setViewportSize(viewport);
		await page.goto(base);
		await installApi(page, { users: [] });
		await transitionModal(page, "createUserModal", "show");

		// No channel yet: fields hidden, Recipient Enabled off and not selectable.
		assert.equal(await visible(page, "new-user-phone"), false);
		assert.equal(await visible(page, "new-user-email"), false);
		assert.equal(await page.isChecked("#new-user-recipient-enabled"), false);
		assert.equal(await page.isDisabled("#new-user-recipient-enabled"), true);

		// A channel switch shows its field and keeps the focus on the switch; Recipient Enabled returns.
		await page.focus("#new-user-signal-enabled");
		await page.keyboard.press("Space");
		assert.equal(await visible(page, "new-user-phone"), true);
		assert.equal(await page.evaluate(() => document.activeElement.id), "new-user-signal-enabled");
		assert.equal(await page.getAttribute("#new-user-signal-enabled", "aria-controls"), "new-user-phone-container");
		assert.equal(await page.isChecked("#new-user-recipient-enabled"), true);
		assert.equal(await page.isDisabled("#new-user-recipient-enabled"), false);

		// A missing value of an enabled channel is a field error and nothing is sent.
		await page.fill("#new-user-username", "neo");
		await page.fill("#new-user-password", "long-enough-password");
		await page.click("#create-user-btn");
		assert.equal(await page.getAttribute("#new-user-phone", "aria-invalid"), "true");
		assert.equal((await posts(page, "POST")).length, 0);

		await page.fill("#new-user-phone", PHONE);
		await page.check("#new-user-all-targets");
		await page.click("#create-user-btn");
		await page.waitForFunction(() => window.__requests.some((r) => r.method === "POST"));
		const [created] = await posts(page, "POST");
		assert.equal(created.url, "/users");
		assert.deepEqual(created.body.recipient, { phone: PHONE, email: null, is_enabled: true, enable_for_all_targets: true });
		assert.equal(created.body.username, "neo");
		assert.equal((await posts(page, "PUT")).length, 0, "user and recipient are one request, not two");
	});
}

test("Create User without a channel sends a disabled recipient and a conflict is shown at the phone field @modal @user-channels", async ({ page, base }) => {
	await page.goto(base);
	await installApi(page, { users: [] });
	await transitionModal(page, "createUserModal", "show");
	await page.fill("#new-user-username", "plain");
	await page.fill("#new-user-password", "long-enough-password");
	await page.click("#create-user-btn");
	await page.waitForFunction(() => window.__requests.some((r) => r.method === "POST"));
	const [created] = await posts(page, "POST");
	assert.deepEqual(created.body.recipient, { phone: null, email: null, is_enabled: false, enable_for_all_targets: false });
	await page.waitForFunction(() => { // the dialog closes after the save and resets itself once hidden
		const modal = document.getElementById("createUserModal");
		return !modal.classList.contains("show") && getComputedStyle(modal).display === "none";
	});

	await installApi(page, { users: [], failWith: { status: 409, detail: "Phone number already exists" } });
	await transitionModal(page, "createUserModal", "show");
	await page.fill("#new-user-username", "dup");
	await page.fill("#new-user-password", "long-enough-password");
	await page.check("#new-user-signal-enabled");
	await page.fill("#new-user-phone", PHONE);
	await page.click("#create-user-btn");
	await page.waitForFunction(() => document.getElementById("new-user-phone").getAttribute("aria-invalid") === "true");
	assert.match(await page.textContent("#createUserModal .ju-field-error, #createUserModal [id$='-error-text'], #createUserModal .invalid-feedback") ?? "", /Phone number already exists/);
});

test("Edit User loads the saved channels, saves them with the user and confirms removal from all targets @modal @user-channels", async ({ page, base }) => {
	await page.goto(base);
	await installApi(page, { users: [USER, BARE] });
	await page.evaluate(() => window.SettingsPage.Users.loadUsers());
	await page.waitForSelector('.edit-user-btn[data-user-id="7"]');

	// Saved values are shown: Signal on with its number, Email off, switches as stored.
	await page.click('.edit-user-btn[data-user-id="7"]');
	await page.waitForFunction(() => document.getElementById("editUserModal").classList.contains("show") && !document.querySelector(".modal-backdrop.fade:not(.show)"));
	assert.equal(await page.isChecked("#edit-user-signal-enabled"), true);
	assert.equal(await page.inputValue("#edit-user-phone"), PHONE);
	assert.equal(await visible(page, "edit-user-phone"), true);
	assert.equal(await page.isChecked("#edit-user-email-enabled"), false);
	assert.equal(await visible(page, "edit-user-email"), false);
	assert.equal(await page.isChecked("#edit-user-recipient-enabled"), true);
	assert.equal(await page.isChecked("#edit-user-all-targets"), true);

	// Add email, drop all-targets: one PUT carries user and recipient; removing the assignments is confirmed first.
	await page.check("#edit-user-email-enabled");
	await page.fill("#edit-user-email", "alice@example.org");
	await page.uncheck("#edit-user-all-targets");
	await page.click("#edit-user-btn");
	await page.waitForFunction(() => window.__requests.some((r) => r.method === "PUT"));
	const [put] = await posts(page, "PUT");
	assert.equal(put.url, "/users/7");
	assert.deepEqual(put.body.recipient, { phone: PHONE, email: "alice@example.org", is_enabled: true, enable_for_all_targets: false });
	assert.deepEqual(await page.evaluate(() => window.__confirms), ["Remove recipient from all targets?"]);
	assert.equal((await posts(page, "PATCH")).length, 0, "no second, independent recipient save");

	// Reload: the stored configuration comes back unchanged in the dialog.
	await page.evaluate(() => window.SettingsPage.Users.loadUsers());
	await page.waitForFunction(() => window.__requests.filter((r) => r.url === "/users" && r.method === "GET").length >= 2);
	await page.evaluate(() => window.SettingsPage.Users.openEditUserModal(7));
	await page.waitForFunction(() => document.getElementById("edit-user-email").value === "alice@example.org");
	assert.equal(await page.isChecked("#edit-user-all-targets"), false);
});

test("Edit User for a user without channels keeps Recipient Enabled off until a channel is switched on @modal @user-channels", async ({ page, base }) => {
	await page.goto(base);
	await installApi(page, { users: [USER, BARE] });
	await page.evaluate(() => window.SettingsPage.Users.loadUsers());
	await page.waitForSelector('.edit-user-btn[data-user-id="8"]');
	await page.click('.edit-user-btn[data-user-id="8"]');
	await page.waitForFunction(() => document.getElementById("editUserModal").classList.contains("show") && !document.querySelector(".modal-backdrop.fade:not(.show)"));
	assert.equal(await page.isChecked("#edit-user-signal-enabled"), false);
	assert.equal(await page.isChecked("#edit-user-email-enabled"), false);
	assert.equal(await page.isChecked("#edit-user-recipient-enabled"), false);
	assert.equal(await page.isDisabled("#edit-user-recipient-enabled"), true);
	await page.click("#edit-user-btn");
	await page.waitForFunction(() => window.__requests.some((r) => r.method === "PUT"));
	const [put] = await posts(page, "PUT");
	assert.deepEqual(put.body.recipient, { phone: null, email: null, is_enabled: false, enable_for_all_targets: false });
	assert.deepEqual(await page.evaluate(() => window.__confirms), [], "nothing to remove, so no confirmation");
});

test("Recipient list marks user recipients and has no active delete button for them @modal @user-channels", async ({ page, base }) => {
	await page.goto(base);
	const recipients = [
		{ id: 41, name: "alice", phone: PHONE, email: null, is_enabled: true, enable_for_all_targets: false, is_user_linked: true, targets: [] },
		{ id: 50, name: "ops", phone: null, email: "ops@example.org", is_enabled: true, enable_for_all_targets: false, is_user_linked: false, targets: [] },
	];
	await installApi(page, { users: [USER], recipients });
	await page.evaluate(() => window.SettingsPage.Recipients.loadRecipients());
	await page.waitForSelector('tr[data-recipient-id="41"]');
	assert.match(await page.textContent('tr[data-recipient-id="41"]'), /Application User/);
	assert.equal(await page.isDisabled('tr[data-recipient-id="41"] .delete-recipient-btn'), true);
	assert.equal(await page.isDisabled('tr[data-recipient-id="50"] .delete-recipient-btn'), false);
	assert.doesNotMatch(await page.textContent('tr[data-recipient-id="50"]'), /Application User/);
	await page.evaluate(() => window.SettingsPage.Recipients.deleteRecipient(41, "alice"));
	assert.equal((await posts(page, "DELETE")).length, 0, "the UI never sends the delete");
});
