//
// app/static/js/utils/link-url.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Validation of a link address typed into the mail template editor. Pure (no DOM), so it is tested
 * on its own; the editor shows the message as a field error (utils/field-errors.js).
 */
(function (root) {
	'use strict';

	// Browsers ignore whitespace and control characters inside a URL scheme, so "java\tscript:" still runs.
	// eslint-disable-next-line no-control-regex
	var HIDDEN = /[\s\u0000-\u001f]/g;
	var SCRIPT_SCHEMES = ['javascript:', 'data:', 'vbscript:'];

	/** @returns {{ok: true, url: string}|{ok: false, error: string}} */
	function validateLinkUrl(raw) {
		var url = raw == null ? '' : String(raw).trim();
		if (!url || /^https?:\/\/$/i.test(url)) return { ok: false, error: 'Enter a web address.' };
		var normalized = url.replace(HIDDEN, '').toLowerCase();
		if (SCRIPT_SCHEMES.some(function (scheme) { return normalized.indexOf(scheme) === 0; })) {
			return { ok: false, error: 'This kind of address is not allowed. Use an http or https address.' };
		}
		return { ok: true, url: url };
	}

	root.JuLinkUrl = { validateLinkUrl: validateLinkUrl };
})(typeof window !== 'undefined' ? window : globalThis);
