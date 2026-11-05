//
// tests/js/confirm-via-modal.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// window.confirmViaModal: promise wrapper for the dedicated Settings confirmation modals.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const SOURCE = readFileSync(new URL("../../app/static/js/utils/confirm-dialog.js", import.meta.url), "utf8");

function target() {
	const listeners = new Map();
	return {
		textContent: "",
		addEventListener(type, fn) { listeners.set(type, fn); },
		removeEventListener(type, fn) { if (listeners.get(type) === fn) listeners.delete(type); },
		fire(type) { listeners.get(type)?.(); },
		listeners,
	};
}

function setup({ withBootstrap = true } = {}) {
	const modalEl = target();
	const confirmBtn = target();
	const nameEl = target();
	const shown = [];
	const els = { m: modalEl, c: confirmBtn, n: nameEl };
	const instance = {
		show() { shown.push("show"); },
		hide() { shown.push("hide"); queueMicrotask(() => modalEl.fire("hidden.bs.modal")); },
	};
	const sandbox = {
		document: { getElementById: (id) => els[id] ?? null },
		bootstrap: withBootstrap ? { Modal: { getOrCreateInstance: () => instance } } : undefined,
	};
	sandbox.window = sandbox;
	vm.runInNewContext(SOURCE, sandbox);
	const ask = () => sandbox.confirmViaModal({ modalId: "m", confirmId: "c", nameId: "n", name: "Alice" });
	return { ask, modalEl, confirmBtn, nameEl, shown };
}

test("confirm resolves true, names the subject in quotes and hides the dialog once", async () => {
	const t = setup();
	const answer = t.ask();
	assert.equal(t.nameEl.textContent, '"Alice"');
	assert.deepEqual(t.shown, ["show"]);
	t.confirmBtn.fire("click");
	assert.equal(await answer, true);
	assert.deepEqual(t.shown, ["show", "hide"]);
	assert.equal(t.confirmBtn.listeners.size, 0, "confirm listener is removed afterwards");
});

test("dismissing without confirming resolves false", async () => {
	const t = setup();
	const answer = t.ask();
	t.modalEl.fire("hidden.bs.modal");
	assert.equal(await answer, false);
	assert.equal(t.confirmBtn.listeners.size, 0);
});

test("missing dialog elements or Bootstrap fail closed", async () => {
	assert.equal(await setup({ withBootstrap: false }).ask(), false);
	const t = setup();
	t.modalEl.listeners.clear();
	const sandbox = { document: { getElementById: () => null }, bootstrap: { Modal: {} } };
	sandbox.window = sandbox;
	vm.runInNewContext(SOURCE, sandbox);
	assert.equal(await sandbox.confirmViaModal({ modalId: "x", confirmId: "y", nameId: "z", name: "n" }), false);
});
