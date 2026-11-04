//
// tests/js/optional-groups.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
const read = (name) => readFileSync(new URL(`../../app/static/js/${name}`, import.meta.url), "utf8");

// Minimal DOM: a recipient dialog with a Signal and an Email channel row (switch + hidden-capable body).
function makeDialog() {
	const focus = { active: null };
	const errors = [];
	const makeChannel = (name) => {
		const field = { id: `${name}-field`, disabled: false, value: "" };
		const body = { id: `${name}-body`, hidden: false, contains: (node) => node === field, field };
		const toggle = {
			id: `${name}-enabled`, checked: false, focus() { focus.active = toggle; },
			getAttribute: (attr) => (attr === "aria-controls" ? body.id : null),
		};
		const row = {
			querySelector: (selector) => (selector === 'input[role="switch"]' ? toggle : null),
			querySelectorAll: () => [field],
		};
		return { toggle, body, field, row };
	};
	const channels = { signal: makeChannel("signal"), email: makeChannel("email") };
	const hint = { hidden: false };
	const all = Object.values(channels);
	const scope = {
		querySelectorAll(selector) {
			if (selector === 'input[role="switch"][aria-controls]' || selector === '.ju-channel input[role="switch"]') return all.map((c) => c.toggle);
			if (selector === ".ju-channel") return all.map((c) => c.row);
			return [];
		},
		querySelector(selector) {
			if (selector === "[data-channel-hint]") return hint;
			const match = /^\[id="(.+)"\]$/.exec(selector);
			return all.find((c) => c.body.id === match?.[1])?.body ?? null;
		},
	};
	const sandbox = {
		window: null,
		document: { addEventListener() {}, get activeElement() { return focus.active; } },
	};
	sandbox.window = sandbox;
	sandbox.JuFieldErrors = { clear: (node) => errors.push(node.id) };
	sandbox.SettingsPage = {};
	vm.createContext(sandbox);
	vm.runInContext(read("utils/optional-groups.js"), sandbox);
	vm.runInContext(read("settings.recipients.js"), sandbox);
	return { channels, hint, scope, errors, focus, sandbox };
}

test("each combination of the two channel switches shows exactly the groups of the active channels", () => {
	for (let mask = 0; mask < 4; mask += 1) {
		const { channels, hint, scope, sandbox } = makeDialog();
		channels.signal.toggle.checked = Boolean(mask & 1);
		channels.email.toggle.checked = Boolean(mask & 2);
		sandbox.syncChannelFields(scope);
		assert.equal(channels.signal.body.hidden, !(mask & 1), `signal body ${mask}`);
		assert.equal(channels.email.body.hidden, !(mask & 2), `email body ${mask}`);
		assert.equal(channels.signal.field.disabled, !(mask & 1), `signal field ${mask}`);
		assert.equal(channels.email.field.disabled, !(mask & 2), `email field ${mask}`);
		assert.equal(hint.hidden, mask === 0, `saved-values hint ${mask}`);
	}
});

test("switching a channel off removes its field error and keeps the entered value", () => {
	const { channels, scope, errors, sandbox } = makeDialog();
	channels.signal.toggle.checked = true;
	channels.signal.field.value = "+49123456789";
	sandbox.syncChannelFields(scope);
	errors.length = 0;
	channels.signal.toggle.checked = false;
	sandbox.syncChannelFields(scope);
	assert.ok(errors.includes("signal-field"), "error of the hidden channel is cleared");
	assert.equal(channels.signal.field.value, "+49123456789");
	channels.signal.toggle.checked = true;
	sandbox.syncChannelFields(scope);
	assert.equal(channels.signal.body.hidden, false);
	assert.equal(channels.signal.field.value, "+49123456789");
});

test("the at-least-one-channel error on the switches clears as soon as a channel is on", () => {
	const { channels, scope, errors, sandbox } = makeDialog();
	sandbox.syncChannelFields(scope);
	assert.ok(!errors.includes("signal-enabled") && !errors.includes("email-enabled"));
	channels.email.toggle.checked = true;
	sandbox.syncChannelFields(scope);
	assert.ok(errors.includes("signal-enabled") && errors.includes("email-enabled"));
});

test("focus inside a group that gets hidden moves to its switch", () => {
	const { channels, scope, focus, sandbox } = makeDialog();
	channels.signal.toggle.checked = true;
	sandbox.syncChannelFields(scope);
	focus.active = channels.signal.field;
	channels.signal.toggle.checked = false;
	sandbox.syncChannelFields(scope);
	assert.equal(focus.active, channels.signal.toggle);
});

test("validation skips switched-off channels but still requires one channel", () => {
	const { sandbox } = makeDialog();
	const run = (input) => Array.from(sandbox.validateRecipientInput({ name: "A", phone: "", email: "", ...input }, "x-"), (e) => e.id);
	assert.deepEqual(run({ signalEnabled: false, emailEnabled: true, email: "a@b.de" }), []);
	assert.deepEqual(run({ signalEnabled: true, emailEnabled: false, phone: "+4912345678" }), []);
	assert.deepEqual(run({ signalEnabled: false, emailEnabled: false }), ["x-signal-enabled"]);
	assert.deepEqual(run({ signalEnabled: true, emailEnabled: true, email: "a@b.de" }), ["x-phone"]);
});

test("apply hides the elements of groups that are not visible (shared with the check groups)", () => {
	const sandbox = { window: null };
	sandbox.window = sandbox;
	vm.createContext(sandbox);
	vm.runInContext(read("utils/optional-groups.js"), sandbox);
	const make = (value) => ({ dataset: { checkGroup: value }, hidden: false });
	const elements = [make("a"), make("b")];
	sandbox.JuOptionalGroups.apply({ querySelectorAll: () => elements }, "check-group", { a: true, b: false });
	assert.deepEqual(elements.map((e) => e.hidden), [false, true]);
});
