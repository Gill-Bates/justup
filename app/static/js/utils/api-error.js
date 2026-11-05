//
// app/static/js/utils/api-error.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
/**
 * Turns a FastAPI error `detail` into one readable message. Pure (no DOM), shared by classic scripts and
 * ES modules so a 422 list never reaches the UI as "[object Object]".
 */
(function (root) {
	'use strict';
	/**
	 * @param {*} detail Value of the response `detail` field (string, {code, message}, or a 422 list).
	 * @param {string} [fallback] Text used when the detail carries nothing readable.
	 * @returns {string}
	 */
	function message(detail, fallback) {
		var otherwise = typeof fallback === 'string' ? fallback : '';
		if (typeof detail === 'string') return detail.trim() ? detail : otherwise;
		if (Array.isArray(detail)) {
			var parts = detail
				.map(function (entry) { return typeof entry === 'string' ? entry : entry && entry.msg; })
				.filter(function (text) { return typeof text === 'string' && text; });
			return parts.length ? parts.join('; ') : otherwise;
		}
		if (detail && typeof detail === 'object' && typeof detail.message === 'string' && detail.message) {
			return detail.message;
		}
		return otherwise;
	}
	/** @returns {string|null} Machine-readable `detail.code`, when the server sent one. */
	function code(detail) {
		return detail && typeof detail === 'object' && !Array.isArray(detail) && typeof detail.code === 'string'
			? detail.code
			: null;
	}
	/**
	 * Message for a parsed JSON error body: `detail`, then `error`, then `message`, then the fallback.
	 * @param {*} body Parsed response body (any shape).
	 * @param {string} [fallback]
	 * @returns {string}
	 */
	function fromBody(body, fallback) {
		var otherwise = typeof fallback === 'string' ? fallback : '';
		if (!body || typeof body !== 'object') return otherwise;
		var text = message(body.detail, Array.isArray(body.detail) ? 'Invalid input' : '');
		if (text) return text;
		if (typeof body.error === 'string' && body.error.trim()) return body.error;
		if (typeof body.message === 'string' && body.message.trim()) return body.message;
		return otherwise;
	}
	root.JuApiError = { message: message, code: code, fromBody: fromBody };
})(typeof window !== 'undefined' ? window : globalThis);
