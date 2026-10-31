//
// app/static/js/utils/tag-input.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Generic tag/chip input. Markup contract (see settings/general.html):
 *   [data-tag-input] root with data-tag-input-defaults (JSON array), -min, -pattern,
 *   -max-length, -max-total, -noun; children [data-tag-input-list] (ul),
 *   [data-tag-input-entry] (text input), [data-tag-input-value] (hidden input that carries
 *   the comma-separated value), [data-tag-input-status] (polite live region),
 *   [data-tag-input-error], optional [data-tag-input-reset] (button).
 * A bubbling "change" event on the hidden input signals a valid, changed value.
 * @module utils/tag-input
 */

import { addTokens, evaluate, initialTokens, removeToken } from "./tag-input-core.js";

function readOptions(root) {
	const num = (name) => Number.parseInt(root.dataset[name] || "", 10) || 0;
	let defaults = [];
	try {
		defaults = JSON.parse(root.dataset.tagInputDefaults || "[]");
	} catch {
		defaults = [];
	}
	return {
		defaults,
		min: num("tagInputMin"),
		maxLength: num("tagInputMaxLength"),
		maxTotal: num("tagInputMaxTotal"),
		pattern: root.dataset.tagInputPattern || "",
	};
}

function setupTagInput(root) {
	const listEl = root.querySelector("[data-tag-input-list]");
	const entry = root.querySelector("[data-tag-input-entry]");
	const hidden = root.querySelector("[data-tag-input-value]");
	const statusEl = root.querySelector("[data-tag-input-status]");
	const errorEl = root.querySelector("[data-tag-input-error]");
	const resetBtn = root.querySelector("[data-tag-input-reset]");
	if (!listEl || !entry || !hidden) return;

	const opts = readOptions(root);
	let list = initialTokens(hidden.value, opts.defaults);
	let committed = hidden.value;
	let pendingRemoval = false;

	const announce = (text) => {
		if (!statusEl) return;
		// Clear first so repeating the same message is announced again.
		statusEl.textContent = "";
		statusEl.textContent = text;
	};

	function render() {
		const result = evaluate(list, opts);
		listEl.textContent = "";
		list.forEach((value, index) => {
			const invalid = result.invalidEntries.includes(value);
			const li = document.createElement("li");
			li.className = "tag-input__item";
			if (invalid) li.classList.add("is-invalid");
			if (pendingRemoval && index === list.length - 1) li.classList.add("is-pending-removal");
			const text = document.createElement("span");
			text.className = "tag-input__text";
			text.textContent = value;
			li.appendChild(text);
			if (invalid) {
				const hint = document.createElement("span");
				hint.className = "visually-hidden";
				hint.textContent = " (invalid)";
				li.appendChild(hint);
			}
			const btn = document.createElement("button");
			btn.type = "button";
			btn.className = "tag-input__remove";
			btn.setAttribute("aria-label", `Remove ${value}`);
			btn.dataset.index = String(index);
			btn.innerHTML = '<span aria-hidden="true">&times;</span>';
			li.appendChild(btn);
			listEl.appendChild(li);
		});
		const hasError = !result.valid;
		root.classList.toggle("has-error", hasError);
		entry.setAttribute("aria-invalid", hasError ? "true" : "false");
		if (errorEl) {
			errorEl.textContent = result.message;
			errorEl.hidden = !hasError;
		}
		if (resetBtn) {
			const atDefaults = result.usingDefaults && !result.isEmpty;
			resetBtn.disabled = atDefaults && committed === "";
		}
		return result;
	}

	function sync() {
		const result = render();
		if (!result.valid) return;
		hidden.value = result.serialized;
		if (result.serialized !== committed) {
			committed = result.serialized;
			hidden.dispatchEvent(new Event("change", { bubbles: true }));
		}
	}

	function commitText(text, { quiet = false } = {}) {
		const { list: next, added, duplicates } = addTokens(list, text);
		list = next;
		pendingRemoval = false;
		sync();
		if (quiet) return;
		const parts = [];
		if (added.length) parts.push(`Added ${added.join(", ")}`);
		if (duplicates.length) parts.push(`Already in list: ${duplicates.join(", ")}`);
		if (parts.length) announce(parts.join(". "));
	}

	function commitEntry() {
		const text = entry.value;
		if (!text.trim()) return;
		entry.value = "";
		commitText(text);
	}

	function removeAt(index) {
		const value = list[index];
		const emptied = list.length === 1;
		list = removeToken(list, index, opts.defaults);
		pendingRemoval = false;
		sync();
		announce(emptied && list.length ? `Removed ${value}. List empty, built-in defaults restored` : `Removed ${value}`);
		const buttons = listEl.querySelectorAll(".tag-input__remove");
		const target = buttons[index] || buttons[index - 1] || entry;
		target.focus();
	}

	listEl.addEventListener("click", (e) => {
		const btn = e.target instanceof Element ? e.target.closest(".tag-input__remove") : null;
		if (btn) removeAt(Number.parseInt(btn.dataset.index || "-1", 10));
	});

	entry.addEventListener("keydown", (e) => {
		const hasText = entry.value.trim() !== "";
		if (e.key === "Backspace" && entry.value === "" && list.length) {
			e.preventDefault();
			if (pendingRemoval) {
				removeAt(list.length - 1);
			} else {
				pendingRemoval = true;
				render();
				announce(`Press Backspace again to remove ${list[list.length - 1]}`);
			}
			return;
		}
		if (pendingRemoval) {
			pendingRemoval = false;
			render();
		}
		if (e.key === "Enter" || e.key === ",") {
			e.preventDefault();
			commitEntry();
		} else if (e.key === "Tab" && !e.shiftKey && hasText) {
			// Only trap Tab while text is pending; an empty field must move focus on.
			e.preventDefault();
			commitEntry();
		}
	});

	// Covers soft keyboards that send no "," keydown and typed separators mid-text.
	entry.addEventListener("input", () => {
		if (!entry.value.includes(",")) return;
		const parts = entry.value.split(",");
		const rest = parts.pop() ?? "";
		entry.value = rest;
		commitText(parts.join(","));
	});

	entry.addEventListener("paste", (e) => {
		const text = e.clipboardData?.getData("text") ?? "";
		if (!/[,\n\r]/.test(text.trim())) return;
		e.preventDefault();
		commitText(text);
	});

	entry.addEventListener("blur", () => {
		if (pendingRemoval) {
			pendingRemoval = false;
			render();
		}
		commitEntry();
	});

	resetBtn?.addEventListener("click", () => {
		list = initialTokens("", opts.defaults);
		entry.value = "";
		pendingRemoval = false;
		sync();
		announce("Reset to defaults");
		entry.focus();
	});

	// Clicking the empty area of the field focuses the entry.
	root.querySelector("[data-tag-input-field]")?.addEventListener("click", (e) => {
		if (e.target === e.currentTarget) entry.focus();
	});

	render();
}

/** Initialise every unbound [data-tag-input] below `scope` (idempotent, HTMX-safe). */
export function initTagInputs(scope = document) {
	scope.querySelectorAll("[data-tag-input]").forEach((root) => {
		if (root.dataset.tagInputBound === "1") return;
		root.dataset.tagInputBound = "1";
		setupTagInput(root);
	});
}
