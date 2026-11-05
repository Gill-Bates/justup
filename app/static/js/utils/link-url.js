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

	/** True when the address starts with a script-capable scheme, ignoring case and hidden characters. */
	function isScriptUrl(raw) {
		var normalized = String(raw == null ? '' : raw).replace(HIDDEN, '').toLowerCase();
		return SCRIPT_SCHEMES.some(function (scheme) { return normalized.indexOf(scheme) === 0; });
	}
	// Raster inline images only: SVG can carry script, so image/svg+xml never matches.
	var RASTER_DATA_URL = /^data:image\/(?:png|jpe?g|gif|webp|bmp|avif);base64,[a-z0-9+\/]*={0,2}$/;
	/** True for a base64 raster data:image/* address, ignoring case and hidden characters. */
	function isRasterImageDataUrl(raw) {
		return RASTER_DATA_URL.test(String(raw == null ? '' : raw).replace(HIDDEN, '').toLowerCase());
	}
	/** True when attribute `name` of element `tag` must be dropped; an img src may keep raster data:image/*. */
	function isBlockedUrlAttribute(tag, name, value) {
		if (String(tag).toLowerCase() === 'img' && name === 'src' && isRasterImageDataUrl(value)) return false;
		return isScriptUrl(value);
	}
	/** @returns {{ok: true, url: string}|{ok: false, error: string}} */
	function validateLinkUrl(raw) {
		var url = raw == null ? '' : String(raw).trim();
		if (!url || /^https?:\/\/$/i.test(url)) return { ok: false, error: 'Enter a web address.' };
		if (isScriptUrl(url)) {
			return { ok: false, error: 'This kind of address is not allowed. Use an http or https address.' };
		}
		return { ok: true, url: url };
	}

	root.JuLinkUrl = { validateLinkUrl: validateLinkUrl, isScriptUrl: isScriptUrl,
		isRasterImageDataUrl: isRasterImageDataUrl, isBlockedUrlAttribute: isBlockedUrlAttribute };
})(typeof window !== 'undefined' ? window : globalThis);
