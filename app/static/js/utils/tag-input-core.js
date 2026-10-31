//
// app/static/js/utils/tag-input-core.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Pure helpers for the tag input (no DOM): parse, normalise, dedupe, validate, serialise.
 * @module utils/tag-input-core
 */

const SEPARATOR_RE = /[,\r\n]+/;

/** Split free text on commas and line breaks; inner spaces stay so they get flagged as invalid. */
export function splitTokens(text) {
	return String(text ?? "").split(SEPARATOR_RE).map((t) => t.trim()).filter(Boolean);
}

export function tokenKey(value) {
	return String(value).trim().toLowerCase();
}

/**
 * Append tokens parsed from `text` to `list`, skipping case-insensitive duplicates.
 * Invalid tokens are kept (the UI flags them) so nothing is swallowed silently.
 * @returns {{list: string[], added: string[], duplicates: string[]}}
 */
export function addTokens(list, text) {
	const next = list.slice();
	const seen = new Set(next.map(tokenKey));
	const added = [];
	const duplicates = [];
	for (const token of splitTokens(text)) {
		const key = tokenKey(token);
		if (seen.has(key)) {
			duplicates.push(token);
			continue;
		}
		seen.add(key);
		next.push(token);
		added.push(token);
	}
	return { list: next, added, duplicates };
}

/** Remove one badge; an emptied list falls back to the defaults, which "" means on the wire. */
export function removeToken(list, index, defaults) {
	const next = list.filter((_, i) => i !== index);
	return next.length ? next : initialTokens("", defaults);
}

/** Initial badges: the stored value, or the built-in defaults when it is empty. */
export function initialTokens(storedValue, defaults) {
	const parsed = addTokens([], storedValue).list;
	return parsed.length ? parsed : addTokens([], (defaults || []).join(",")).list;
}

export function sameList(a, b) {
	return a.length === b.length && a.every((v, i) => tokenKey(v) === tokenKey(b[i]));
}

/** Mirrors the backend entry rule (pattern + max length). */
export function isValidEntry(value, { pattern, maxLength } = {}) {
	if (maxLength && value.length > maxLength) return false;
	if (!pattern) return true;
	return new RegExp(pattern).test(value);
}

/**
 * Wire value: "" when the list equals the defaults (keeps "empty = built-in defaults"),
 * otherwise the comma-separated list.
 */
export function serialize(list, defaults) {
	if (!list.length || sameList(list, defaults || [])) return "";
	return list.join(",");
}

/**
 * Judge a list against the backend rules.
 * Zero badges is valid (saved as "", backend then uses the defaults);
 * 1..min-1 badges, an invalid entry, or an oversized total is an error and must not be saved.
 */
export function evaluate(list, { defaults = [], min = 0, pattern, maxLength, maxTotal } = {}) {
	const invalidEntries = list.filter((v) => !isValidEntry(v, { pattern, maxLength }));
	const serialized = serialize(list, defaults);
	const belowMin = list.length > 0 && list.length < min;
	const tooLong = Boolean(maxTotal) && serialized.length > maxTotal;
	let message = "";
	if (invalidEntries.length) {
		message = `Invalid ${invalidEntries.length === 1 ? "entry" : "entries"}: ${invalidEntries.join(", ")}. Remove or correct to save.`;
	} else if (belowMin) {
		message = `At least ${min} targets are required (${list.length} present). Not saved until fixed.`;
	} else if (tooLong) {
		message = `The list is too long (max ${maxTotal} characters).`;
	}
	return {
		valid: !message,
		message,
		serialized,
		invalidEntries,
		belowMin,
		usingDefaults: serialized === "",
		isEmpty: list.length === 0,
	};
}
